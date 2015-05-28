# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members


import time
import urllib

from twisted.internet import defer
from twisted.web import html
from twisted.web.resource import NoResource
from twisted.web.util import Redirect

from buildbot import util
from buildbot.status.web.base import ActionResource
from buildbot.status.web.base import BuildLineMixin
from buildbot.status.web.base import HtmlResource
from buildbot.status.web.base import abbreviate_age
from buildbot.status.web.base import path_to_authzfail
from buildbot.status.web.base import path_to_slave


class ShutdownActionResource(ActionResource):

    def __init__(self, slave):
        self.slave = slave
        self.action = "gracefulShutdown"

    @defer.inlineCallbacks
    def performAction(self, request):
        res = yield self.getAuthz(request).actionAllowed(self.action,
                                                         request,
                                                         self.slave)

        url = None
        if res:
            self.slave.setGraceful(True)
            url = path_to_slave(request, self.slave)
        else:
            url = path_to_authzfail(request)
        defer.returnValue(url)


class PauseActionResource(ActionResource):

    def __init__(self, slave, state):
        self.slave = slave
        self.action = "pauseSlave"
        self.state = state

    @defer.inlineCallbacks
    def performAction(self, request):
        res = yield self.getAuthz(request).actionAllowed(self.action,
                                                         request,
                                                         self.slave)

        url = None
        if res:
            self.slave.setPaused(self.state)
            url = path_to_slave(request, self.slave)
        else:
            url = path_to_authzfail(request)
        defer.returnValue(url)

# /buildslaves/$slavename


class OneBuildSlaveResource(HtmlResource, BuildLineMixin):
    addSlash = False

    def __init__(self, slavename):
        HtmlResource.__init__(self)
        self.slavename = slavename

    def getPageTitle(self, req):
        return "Buildbot: %s" % self.slavename

    def getChild(self, path, req):
        s = self.getStatus(req)
        slave = s.getSlave(self.slavename)
        if path == "shutdown":
            return ShutdownActionResource(slave)
        if path == "pause" or path == "unpause":
            return PauseActionResource(slave, path == "pause")
        return Redirect(path_to_slave(req, slave))

    def content(self, request, ctx):
        s = self.getStatus(request)
        slave = s.getSlave(self.slavename)

        my_builders = []
        for bname in s.getBuilderNames():
            b = s.getBuilder(bname)
            for bs in b.getSlaves():
                if bs.getName() == self.slavename:
                    my_builders.append(b)

        # Current builds
        current_builds = []
        for b in my_builders:
            for cb in b.getCurrentBuilds():
                if cb.getSlavename() == self.slavename:
                    current_builds.append(self.get_line_values(request, cb))

        try:
            max_builds = int(request.args.get('numbuilds')[0])
        except ValueError:
            max_builds = 10

        recent_builds = []
        n = 0
        for rb in s.generateFinishedBuilds(builders=[b.getName() for b in my_builders]):
            if rb.getSlavename() == self.slavename:
                n += 1
                recent_builds.append(self.get_line_values(request, rb))
                if n > max_builds:
                    break

        # connects over the last hour
        slave = s.getSlave(self.slavename)
        connect_count = slave.getConnectCount()

        if slave.isPaused():
            pause_url = request.childLink("unpause")
        else:
            pause_url = request.childLink("pause")

        ctx.update(dict(slave=slave,
                        slavename=self.slavename,
                        current=current_builds,
                        recent=recent_builds,
                        shutdown_url=request.childLink("shutdown"),
                        pause_url=pause_url,
                        authz=self.getAuthz(request),
                        this_url="../../../" + path_to_slave(request, slave),
                        access_uri=slave.getAccessURI(),
                        admin=slave.getAdmin() or u'',
                        host=slave.getHost() or u'',
                        info=slave.getInfoAsDict(),
                        slave_version=slave.getVersion(),
                        show_builder_column=True,
                        connect_count=connect_count))
        template = request.site.buildbot_service.templates.get_template("buildslave.html")
        data = template.render(**ctx)
        return data

# /buildslaves


class BuildSlavesResource(HtmlResource):
    pageTitle = "BuildSlaves"
    addSlash = True

    def content(self, request, ctx):
        s = self.getStatus(request)

        # ?no_builders=1 disables build column
        show_builder_column = not (request.args.get('no_builders', '0')[0]) == '1'
        ctx['show_builder_column'] = show_builder_column

        used_by_builder = {}
        for bname in s.getBuilderNames():
            b = s.getBuilder(bname)
            for bs in b.getSlaves():
                slavename = bs.getName()
                if slavename not in used_by_builder:
                    used_by_builder[slavename] = []
                used_by_builder[slavename].append(bname)

        slaves = ctx['slaves'] = []
        for name in util.naturalSort(s.getSlaveNames()):
            info = {}
            slaves.append(info)
            slave = s.getSlave(name)
            slave_status = s.botmaster.slaves[name].slave_status
            info['running_builds'] = len(slave_status.getRunningBuilds())
            info['link'] = request.childLink(urllib.quote(name, ''))
            info['name'] = name

            if show_builder_column:
                info['builders'] = []
                for b in used_by_builder.get(name, []):
                    info['builders'].append(dict(link=request.childLink("../builders/%s" % b), name=b))

            info['version'] = slave.getVersion()
            info['connected'] = slave.isConnected()
            info['connectCount'] = slave.getConnectCount()
            info['dutyCycles'] = determine_duty_cycle(name, s, slave)
            info['paused'] = slave.isPaused()

            info['admin'] = slave.getAdmin() or u''
            last = slave.lastMessageReceived()
            if last:
                info['last_heard_from_age'] = abbreviate_age(time.time() - last)
                info['last_heard_from_time'] = time.strftime("%Y-%b-%d %H:%M:%S",
                                                             time.localtime(last))

        template = request.site.buildbot_service.templates.get_template("buildslaves.html")
        data = template.render(**ctx)
        return data

    def getChild(self, path, req):
        try:
            self.getStatus(req).getSlave(path)
            return OneBuildSlaveResource(path)
        except KeyError:
            return NoResource("No such slave '%s'" % html.escape(path))


def between(a, n, b):
    if a <= n and n < b:
        return True


def add_to_buckets(num_buckets, buckets, bucket_size, start, end, now):
    for n in range(num_buckets):
        bucket_start = now - (n + 1) * bucket_size
        bucket_end   = now -  n      * bucket_size

        start_before_bucket = start < bucket_start
        start_after_bucket = start > bucket_end
        start_in_bucket = between(bucket_start, start, bucket_end)

        end_before_bucket = end < bucket_start
        end_after_bucket = end > bucket_end
        end_in_bucket = between(bucket_start, end, bucket_end)

        if end_before_bucket or start_after_bucket:
            continue

        if start_in_bucket and end_in_bucket:
            buckets[n] += end - start
        elif start_in_bucket:
            buckets[n] += bucket_end - start
        elif end_in_bucket:
            buckets[n] += end - bucket_start
        elif start_before_bucket and end_after_bucket:
            buckets[n] += 1


def determine_duty_cycle(name, status, slave):
    # Compute numbers for the last week.
    num_buckets = 7
    buckets = [0] * num_buckets

    now = util.now()
    bucket_size = 60 * 60 * 24

    bucket_reach = now - num_buckets * bucket_size

    my_builders = []
    for bname in status.getBuilderNames():
        b = status.getBuilder(bname)
        for bs in b.getSlaves():
            if bs.getName() == name:
                my_builders.append(b)

    # Current builds
    current_builds = []
    for b in my_builders:
        for cb in b.getCurrentBuilds():
            if cb.getSlavename() == name:
                start, end = cb.getTimes()
                add_to_buckets(num_buckets, buckets, bucket_size, start, end, now)

    for rb in status.generateFinishedBuilds(builders=[b.getName() for b in my_builders]):
        if rb.getSlavename() == name:
            start, end = rb.getTimes()
            if end < bucket_reach:
                break
            add_to_buckets(num_buckets, buckets, bucket_size, start, end, now)

    for n in range(num_buckets):
        buckets[n] /= bucket_size

    return buckets
