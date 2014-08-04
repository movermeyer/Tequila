"""
Tequila: a command-line Minecraft server manager written in python

Copyright (C) 2014 Snaipe

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
from distutils.dir_util import copy_tree, remove_tree
from enum import Enum
from os import makedirs
from os.path import join, exists
from shutil import chown
from subprocess import call
from tempfile import gettempdir
import time

from .control import Controlled
from .exception import ServerRunningException, ServerException
from .wrapper import Wrapper

from ..daemonize import fork_and_daemonize
from ..util import delegate, do_as_user


def init_copy(instance):
    copy_tree(instance.server.home, instance.home)


def delete_copy(instance):
    remove_tree(instance.home)


def init_union(instance):
    makedirs(instance.home, mode=0o755, exist_ok=True)
    chown(instance.home, user=instance.server.config.get_user())
    call(['mount', '-t', 'aufs', '-o',
          'br=%s:%s' % (instance.home, instance.server.home),
          'none', instance.home])


def delete_union(instance):
    call(['umount', instance.home])
    remove_tree(instance.home)


class InstancePolicy(Enum):
    copy = (init_copy, delete_copy)
    union = (init_union, delete_union)

    @classmethod
    def from_string(cls, string):
        return getattr(cls, string.lower(), None) if string else None


class BindingPolicy(Enum):
    fixed = 0
    dynamic = 1

    @classmethod
    def from_string(cls, string):
        return getattr(cls, string.lower(), None) if string else None


class InstanceNotCleanException(ServerException):
    def __init__(self, instance):
        super().__init__('Instance #$id of server $name has not been cleaned up (did the watchdog crash?). '
                         'Please manually delete the directory $dir',
                         instance.server, id=instance.instance_id, dir=instance.home)


class ServerInstance(Controlled):

    def __init__(self, server, id):
        from . import ServerControl
        from copy import copy

        self.instance_directory = join(gettempdir(), 'tequila_instances', server.name)
        self.instance_id = self.get_id(id)

        self.server = copy(server)
        self.server.home = self.home = join(self.instance_directory, str(self.instance_id))

        super().__init__(server.name, ServerControl(server))
        self.control_interface.wrapper = InstanceWrapper(self, self.control_interface.wrapper)
        self.server = server

        delegate(self, self.control_interface)

    def find_available_port(self, low, high):
        pass

    def get_id(self, instance_id):
        if instance_id > 0:
            return instance_id

        i = 1
        root = self.instance_directory
        makedirs(root, exist_ok=True)

        directory = join(root, str(i))
        while exists(directory):
            i += 1
            directory = join(root, str(i))

        return i

    def run(self):
        if self.control_interface.wrapper.running():
            raise ServerRunningException(self.server)

        if exists(self.home):
            raise InstanceNotCleanException(self)

        if fork_and_daemonize():
            from .wrapper import waitpid

            init, delete = self.server.config.get_instance_policy().value
            init(self)

            do_as_user(self.server.config.get_user(), self.control_interface.start)

            waitpid(self.control_interface.wrapper.pid(), dt=5)

            # the extra sleeping is apparently required to let the system claim back the resources.
            time.sleep(1)
            delete(self)


class InstanceWrapper(Wrapper):

    def __init__(self, instance, wrapper):
        self.instance = instance
        self.wrapper = wrapper.__class__(instance.server, '')
        # hack in a replacement ID
        self.wrapper.wrapper_id = wrapper.wrapper_id + '_' + str(instance.instance_id)

        super().__init__(instance.server, self.wrapper.wrapper_id)

        delegate(self, self.wrapper)

    def port(self):
        port_range = self.server.config.get_instance_port_range()
        low = max(1 << 10, range[0] if len(port_range) > 0 else 1 << 10)
        high = max(low, range[1] if len(port_range) > 1 else 1 << 16)

        if self.server.config.get_instance_binding_policy() == BindingPolicy.dynamic:
            return self.instance.find_available_port(low, high)
        else:
            return min(high, low + self.instance.instance_id)

    def get_jvm_opts(self, **kwargs):
        return self.server.get_jvm_opts(**kwargs)

    def get_server_opts(self, **kwargs):
        kwargs['port'] = str(self.port())
        kwargs['instance_count'] = str(self.instance.instance_id)
        return self.server.get_server_opts(**kwargs)