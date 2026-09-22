#!/usr/bin/env python3
"""Agent d'appairage BlueZ auto-acceptant (Just Works), RÉSILIENT aux redémarrages
de bluetoothd : surveille org.bluez sur le bus système et se ré-enregistre à chaque
apparition d'un nouveau bluetoothd.
"""
import sys

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib

LOG = "/tmp/oura-agent-dbus.log"
AGENT_PATH = "/oura/agent"
CAPABILITY = "NoInputNoOutput"
BLUEZ = "org.bluez"


def log(msg):
    with open(LOG, "a") as f:
        f.write(msg + "\n")
    print(msg, flush=True)


class OuraAgent(dbus.service.Object):
    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        log("Release()")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        log(f"AuthorizeService({device}, {uuid}) -> OK")
        return

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        log(f"RequestAuthorization({device}) -> OK")
        return

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        log(f"RequestConfirmation({device}, passkey={passkey:06d}) -> OK")
        return

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        log(f"RequestPinCode({device}) -> 0000")
        return "0000"

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        log(f"RequestPasskey({device}) -> 0")
        return dbus.UInt32(0)

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        log(f"DisplayPasskey({device}, {passkey:06d}, {entered})")

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        log(f"DisplayPinCode({device}, {pincode})")

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        log("Cancel()")


def register(bus, obj):
    """(Ré-)enregistre l'agent auprès du bluetoothd courant."""
    try:
        manager = dbus.Interface(bus.get_object(BLUEZ, "/org/bluez"),
                                 "org.bluez.AgentManager1")
        manager.RegisterAgent(AGENT_PATH, CAPABILITY)
        manager.RequestDefaultAgent(AGENT_PATH)
        log("Agent (ré-)enregistré (NoInputNoOutput) — default agent")
    except dbus.exceptions.DBusException as e:
        log(f"enregistrement échoué: {e}")


def main():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    obj = OuraAgent(bus, AGENT_PATH)

    def on_name_owner_changed(name, old, new):
        if name == BLUEZ:
            log(f"org.bluez changé (old={old} new={new}) -> ré-enregistrement")
            if new:
                GLib.timeout_add(1500, lambda: (register(bus, obj), False)[1])

    bus.add_signal_receiver(on_name_owner_changed,
                            signal_name="NameOwnerChanged",
                            dbus_interface="org.freedesktop.DBus",
                            arg0=BLUEZ)

    register(bus, obj)
    GLib.MainLoop().run()


if __name__ == "__main__":
    sys.exit(main())