# devices.py
#
# Copyright 2021 Martin Abente Lahaye
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from gi.repository import Gio, GLib, GObject

from . import logger


class PortfolioDrive(GObject.GObject):
    __gtype_name__ = "PortfolioDrive"

    def __init__(self, object):
        GObject.GObject.__init__(self)

        self._object = object
        self._drive_proxy = object.get_interface("org.freedesktop.UDisks2.Drive")

        self.uuid = self._get_drive_uuid()
        self.is_ejectable = self._get_drive_is_ejectable()
        self.can_power_off = self._get_drive_can_power_off()

    def __repr__(self):
        return f"Drive(uuid={self.uuid}, is_ejectable={self.is_ejectable}, can_power_off={self.can_power_off})"

    def _get_drive_uuid(self):
        return self._drive_proxy.get_cached_property("Id").unpack()

    def _get_drive_is_ejectable(self):
        return self._drive_proxy.get_cached_property("Ejectable").unpack()

    def _get_drive_can_power_off(self):
        return self._drive_proxy.get_cached_property("CanPowerOff").unpack()

    def eject(self):
        self._drive_proxy.Eject("(a{sv})", ({}))

    def power_off(self):
        self._drive_proxy.PowerOff("(a{sv})", ({}))


class PortfolioBlock(GObject.GObject):
    __gtype_name__ = "PortfolioBlock"

    __gsignals__ = {
        "updated": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, object):
        GObject.GObject.__init__(self)

        self._object = object
        self._block_proxy = object.get_interface("org.freedesktop.UDisks2.Block")

        self.label = self._get_block_label()
        self.uuid = self._get_block_uuid()
        self.drive = self._get_block_drive()

    def __repr__(self):
        return f"Block(uuid={self.uuid}, label={self.label})"

    def _get_block_drive(self):
        return self._block_proxy.get_cached_property("Drive").unpack()

    def _get_block_label(self):
        for property in ["IdLabel", "IdUUID"]:
            if label := self._block_proxy.get_cached_property(property):
                return label.unpack()

        return None

    def _get_block_uuid(self):
        if uuid := self._block_proxy.get_cached_property("IdUUID"):
            return uuid.unpack()

        return None


class PortfolioDevice(PortfolioBlock):
    __gtype_name__ = "PortfolioDevice"

    def __init__(self, object):
        PortfolioBlock.__init__(self, object)

        self._filesystem_proxy = object.get_interface(
            "org.freedesktop.UDisks2.Filesystem"
        )
        self._filesystem_proxy.connect(
            "g-properties-changed", self._on_filesystem_changed
        )

        self.mount_point = self._get_filesystem_mount_point()

    def __repr__(self):
        return f"Device(uuid={self.uuid}, label={self.label}, mount_point={self.mount_point})"

    def _get_string_from_bytes(self, bytes):
        return bytearray(bytes).replace(b"\x00", b"").decode("utf-8")

    def _get_filesystem_mount_point(self):
        mount_points = [
            self._get_string_from_bytes(m)
            for m in self._filesystem_proxy.get_cached_property("MountPoints")
            if m
        ]

        if mount_points:
            return mount_points[0]

        return None

    def _on_filesystem_changed(self, proxy, new_properties, old_properties):
        properties = new_properties.unpack()
        if "MountPoints" in properties:
            self.mount_point = self._get_filesystem_mount_point()
            self.emit("updated")

    def mount(self):
        return self._filesystem_proxy.Mount("(a{sv})", ({}))

    def unmount(self):
        return self._filesystem_proxy.Unmount("(a{sv})", ({}))


class PortfolioEncrypted(PortfolioBlock):
    __gtype_name__ = "PortfolioEncrypted"

    __gsignals__ = {
        "finished": (GObject.SignalFlags.RUN_LAST, None, ()),
        "failed": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, object):
        PortfolioBlock.__init__(self, object)

        self._encrypted_proxy = object.get_interface(
            "org.freedesktop.UDisks2.Encrypted"
        )
        self._encrypted_proxy.connect(
            "g-properties-changed", self._on_cleartext_device_changed
        )

        self.mount_point = None
        self.cleartext_device = self._get_encrypted_cleartext_device()

    def _get_encrypted_cleartext_device(self):
        return self._encrypted_proxy.get_cached_property("CleartextDevice").unpack()

    def _on_cleartext_device_changed(self, proxy, new_properties, old_properties):
        properties = new_properties.unpack()
        if "CleartextDevice" in properties:
            self.cleartext_device = self._get_encrypted_cleartext_device()
            self.emit("updated")

    def _unlock_finish(self, proxy, task, data):
        if task.had_error():
            self.emit("failed")
        else:
            self.emit("finished")

    def unlock(self, passphrase):
        self._encrypted_proxy.call(
            "Unlock",
            GLib.Variant("(sa{sv})", (passphrase, {})),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            self._unlock_finish,
            None,
        )


class PortfolioDevices(GObject.GObject):
    __gtype_name__ = "PortfolioDevices"

    __gsignals__ = {
        "added": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "removed": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "encrypted-added": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(self):
        GObject.GObject.__init__(self)

        self._drives = {}
        self._devices = {}
        self._encrypted = {}
        self._manager = None

        try:
            self._manager = self._get_manager_proxy()
        except Exception as e:
            logger.debug(f"No udisk2 service found: {e}")
            return

        self._manager.connect("object-added", self._on_object_added)
        self._manager.connect("object-removed", self._on_object_removed)

    def _get_manager_proxy(self):
        return Gio.DBusObjectManagerClient.new_for_bus_sync(
            Gio.BusType.SYSTEM,
            Gio.DBusObjectManagerClientFlags.NONE,
            "org.freedesktop.UDisks2",
            "/org/freedesktop/UDisks2",
            None,
            None,
            None,
        )

    def _on_object_added(self, manager, object):
        self._add_object(object)

    def _on_object_removed(self, manager, object):
        self._remove_object(object)

    def _on_encrypted_updated(self, encrypted):
        if encrypted.cleartext_device in self._devices:
            self.emit("removed", encrypted)
            del self._encrypted[encrypted._object.get_object_path()]

    def _add_object(self, object):
        if drive := object.get_interface("org.freedesktop.UDisks2.Drive"):
            self._drives[drive.get_object_path()] = PortfolioDrive(object)
        elif device := object.get_interface("org.freedesktop.UDisks2.Filesystem"):
            self._devices[device.get_object_path()] = PortfolioDevice(object)
            self.emit("added", self._devices[device.get_object_path()])
        elif proxy := object.get_interface("org.freedesktop.UDisks2.Encrypted"):
            encrypted = PortfolioEncrypted(object)
            encrypted.connect("updated", self._on_encrypted_updated)
            self._encrypted[proxy.get_object_path()] = encrypted
            if encrypted.cleartext_device == "/":
                self.emit("encrypted-added", encrypted)

    def _remove_object(self, object):
        if drive := object.get_interface("org.freedesktop.UDisks2.Drive"):
            del self._drives[drive.get_object_path()]
        elif device := object.get_interface("org.freedesktop.UDisks2.Filesystem"):
            self.emit("removed", self._devices[device.get_object_path()])
            del self._devices[device.get_object_path()]
        elif encrypted := object.get_interface("org.freedesktop.UDisks2.Encrypted"):
            if encrypted.get_object_path() not in self._encrypted:
                return
            self.emit("removed", self._encrypted[encrypted.get_object_path()])
            del self._encrypted[encrypted.get_object_path()]

    def scan(self):
        if self._manager is None:
            return
        for object in self._manager.get_objects():
            self._add_object(object)

    def can_remove_drive(self, device):
        drive = self._drives.get(device.drive)

        if drive is None:
            return False

        return drive.is_ejectable and drive.can_power_off

    def remove_drive(self, device):
        drive = self._drives.get(device.drive)

        if drive is None:
            return
        if drive.is_ejectable:
            drive.eject()
        if not drive.can_power_off:
            drive.power_off()