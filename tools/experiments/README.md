# Signed override experiments — the rule that was learned the hard way

A signed script override runs **as root, on every start of its service, on a bridge that may
be carrying a live session**. That is the right amount of power for pushing a fix to hardware
in another building, and it is more than enough to take a working bridge down.

## The rule

> **An override that changes system state must act at most ONCE, and must prove it acted
> before it is allowed to try again.**

Write a marker to `/data` before the risky operation, and gate on the marker — never on
whether the change *appears* to have worked. Those are different things, and the difference
is an outage.

## What went wrong on 2026-08-13

An override raised the UAC2 gadget's `req_number` by unbinding the UDC, writing the new
value, and rebinding. It gated on `current != wanted`:

```bash
if [ "$CUR" != "$WANT" ]; then      # <-- the bug is this line
  unbind; write; rebind
fi
```

configfs **refused the write** — that attribute is read-only while the function is linked
into a configuration, and unbinding the UDC is not sufficient. So `CUR` stayed 8, the
condition stayed true, and every restart of the service unbound and rebound the gadget
again. The meeting laptop's camera, microphone and speakers dropped repeatedly, the bridge
fell off the network, and the queued `revert-script` could not be delivered until it was
power-cycled.

Nothing crashed, so **auto-rollback never fired** — the quarantine mechanism watches for a
script that dies three times in two minutes, and this one exited cleanly every time while
doing damage. A loop that succeeds is invisible to it.

## The shape that is safe

```bash
MARK=/data/.experiment-<name>.done
if [ ! -f "$MARK" ]; then
  : > "$MARK"          # BEFORE the risky operation, never after
  sync
  ... do the thing once ...
fi
exec /usr/local/bin/<the baked-in script> "$@"
```

Writing the marker first is deliberate. If the operation wedges the bridge and it is
power-cycled, the marker is already on disk and the next boot does **not** repeat it. A
marker written afterwards is a marker that never gets written on exactly the run that hurt.

## Also worth knowing

- **Delegate, never duplicate.** End with `exec` on the baked-in script rather than pasting a
  copy of the media pipeline. Two audio outages in this project came from edited pipeline
  copies (`S16LE` on a branch that would not link, and an empty variable leaving `! !`), and
  a copy goes stale the moment the real script changes.
- **Test convergence, not syntax.** `bash -n` proves nothing about whether a loop terminates.
  Run it twice and confirm the second run is a no-op.
- **Prefer the image for anything set at boot.** `req_number`, `c_srate` and the gadget
  descriptor are written once by `uvc-raw-setup.sh` before the function is linked. That is
  the only place they can be changed, and an override cannot reach it.
- **A queued revert is not a safety net if the bridge goes offline.** It only lands when the
  agent next checks in.
