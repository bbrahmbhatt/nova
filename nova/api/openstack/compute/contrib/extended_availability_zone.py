# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2013 Netease, LLC.
# All Rights Reserved.
#
#   Licensed under the Apache License, Version 2.0 (the "License"); you may
#   not use this file except in compliance with the License. You may obtain
#   a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#   WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#   License for the specific language governing permissions and limitations
#   under the License.

"""The Extended Availability Zone Status API extension."""

from nova.api.openstack import extensions
from nova.api.openstack import wsgi
from nova.api.openstack import xmlutil
from nova import availability_zones
from nova.openstack.common import log as logging

LOG = logging.getLogger(__name__)
authorize = extensions.soft_extension_authorizer('compute',
                                                 'extended_availability_zone')


class ExtendedAZController(wsgi.Controller):

    def _get_host_az(self, context, instance):
        admin_context = context.elevated()
        if instance['host']:
            return availability_zones.get_host_availability_zone(
                                            admin_context, instance['host'])

    def _extend_server(self, context, server, instance):
        key = "%s:availability_zone" % Extended_availability_zone.alias
        server[key] = instance.get('availability_zone', None)

        key = "%s:host_availability_zone" % Extended_availability_zone.alias
        server[key] = self._get_host_az(context, instance)

    @wsgi.extends
    def show(self, req, resp_obj, id):
        context = req.environ['nova.context']
        if authorize(context):
            resp_obj.attach(xml=ExtendedAZTemplate())
            server = resp_obj.obj['server']
            db_instance = req.get_db_instance(server['id'])
            self._extend_server(context, server, db_instance)

    @wsgi.extends
    def detail(self, req, resp_obj):
        context = req.environ['nova.context']
        if authorize(context):
            resp_obj.attach(xml=ExtendedAZsTemplate())
            servers = list(resp_obj.obj['servers'])
            for server in servers:
                db_instance = req.get_db_instance(server['id'])
                self._extend_server(context, server, db_instance)


class Extended_availability_zone(extensions.ExtensionDescriptor):
    """Extended Server Attributes support."""

    name = "ExtendedAvailabilityZone"
    alias = "OS-EXT-AZ"
    namespace = ("http://docs.openstack.org/compute/ext/"
                 "extended_availability_zone/api/v2")
    updated = "2013-01-30T00:00:00+00:00"

    def get_controller_extensions(self):
        controller = ExtendedAZController()
        extension = extensions.ControllerExtension(self, 'servers', controller)
        return [extension]


def make_server(elem):
    elem.set('{%s}availability_zone' % Extended_availability_zone.namespace,
             '%s:availability_zone' % Extended_availability_zone.alias)
    elem.set('{%s}host_availability_zone' %
             Extended_availability_zone.namespace,
             '%s:host_availability_zone' %
             Extended_availability_zone.alias)


class ExtendedAZTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('server', selector='server')
        make_server(root)
        alias = Extended_availability_zone.alias
        namespace = Extended_availability_zone.namespace
        return xmlutil.SlaveTemplate(root, 1, nsmap={alias: namespace})


class ExtendedAZsTemplate(xmlutil.TemplateBuilder):
    def construct(self):
        root = xmlutil.TemplateElement('servers')
        elem = xmlutil.SubTemplateElement(root, 'server', selector='servers')
        make_server(elem)
        alias = Extended_availability_zone.alias
        namespace = Extended_availability_zone.namespace
        return xmlutil.SlaveTemplate(root, 1, nsmap={alias: namespace})
