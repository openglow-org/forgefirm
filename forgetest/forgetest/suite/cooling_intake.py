# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""cooling.intake-one-fan - a machine built with one intake fan
(cool_intake_fans 1, which the setup's Intake fans step writes when the
operator counts one): the second
intake tach is published as not fitted and never judged, and intake 1
still holds the intake floor. On a machine with two intake fans the
setting alone proves both halves: intake 2 turns and is not judged, and
an intake floor no fan can meet trips with intake 2 still out of it."""
from ..catalog import test
from .cooling import _COOL_COVERS, _after_session, _cool, _fan_gate, _run_session, _set_gates

INTAKE_KEYS = ("cool_intake_fans", "cool_tach_intake_min_rpm", "cool_fan_grace_s")
INTAKE_GRACE_S = "8"        # past the intakes' 7 s to 90 percent, so only the leg's floor trips
INTAKE_TRIP_WAIT_S = 20     # grace + three ticks, with slack for the 1 Hz pipeline


@test("cooling.intake-one-fan", title="With one intake fan set, intake 2 is not judged and intake 1 still is",
      subsystem="cooling", kind="auto", mode="grbl", est_min=3,
      covers=_COOL_COVERS + [("forgectrl", "src/main.c")], requires=["cooling.fan-gate-trips"],
      steps=["Machine idle, fans quiet. The test sets one intake fan, the intake floor and the spin-up "
             "grace and restores all three; three short M8/M9 cycles spin the fans."],
      description="cool_intake_fans 1 must take intake 2 out of the airflow gates and leave intake 1 in "
                  "them. With it set, a session at the cut fan profile must publish intake_2 as not fitted "
                  "with no floor while it reads its real speed, run OK, and judge intake_1 ok at the intake "
                  "floor; an intake floor at its legal maximum must then trip AIRFLOW (hold, fire blocked) "
                  "with intake_1 TRIPPED and intake_2 still not fitted, and the fault must end with the "
                  "session; restored, the next session judges intake_2 again and every fan reads at or "
                  "above its floor.")
def intake_one_fan(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    before = fc.settings()
    orig = {k: before.get(k, "") for k in INTAKE_KEYS}
    ev["orig"] = orig
    ctx.log("original: %s", orig)
    inl = (before.get("gates") or {}).get("cool_tach_intake_min_rpm") or {}
    ctx.check(inl.get("gate") == "intake" and inl.get("off") == "low",
              "settings reply does not describe the intake floor as the intake gate, off at zero: %s", inl)
    ctx.check(orig["cool_intake_fans"] != "1",
              "cool_intake_fans is already 1: the test starts from a machine with both intakes judged")
    c0 = _cool(fc)
    ctx.check(c0.get("verdict") == "OK" and c0.get("gates_off") == [],
              "engine is not at OK with every gate on before the test: %s", c0)

    restored = False
    with ctx.grbl() as grbl:
        st = grbl.status_report()
        ctx.check(st["state"].startswith("Idle"), "controller is %s", st["state"])
        try:
            # Leg 1: one intake fan. Intake 2 still turns on this machine; it
            # is measured and published not fitted, and nothing judges it.
            _set_gates(ctx, fc, {"cool_intake_fans": "1", "cool_fan_grace_s": INTAKE_GRACE_S})
            c = _run_session(ctx, grbl, fc,
                             lambda c: c.get("verdict") == "OK" and _fan_gate(c, "intake_1").get("state") == "ok",
                             "one intake fan leg", wait=INTAKE_TRIP_WAIT_S + 10)
            g1, g2 = _fan_gate(c, "intake_1"), _fan_gate(c, "intake_2")
            ev["one_fan"] = {"cool": c, "intake_1": g1, "intake_2": g2}
            ctx.check(g2.get("state") == "not fitted" and (g2.get("floor") or 0) == 0,
                      "intake_2 is not published not fitted with no floor: %s", g2)
            ctx.check((g2.get("reading") or 0) > 0,
                      "intake_2 reads %s: the test needs a machine whose second intake turns", g2.get("reading"))
            ctx.check(c.get("verdict") == "OK", "the one intake fan session is not OK: %s", c)
            ctx.check(g1.get("state") == "ok" and (g1.get("floor") or 0) > 0,
                      "intake_1 is not judged ok at the intake floor: %s", g1)

            # Leg 2: the intake floor at its legal maximum. Intake 1 is the
            # one intake left in the gates; intake 2 stays out of them.
            _set_gates(ctx, fc, {"cool_tach_intake_min_rpm": str(inl.get("hi"))})
            c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "AIRFLOW", "intake 1 trip leg",
                             wait=INTAKE_TRIP_WAIT_S)
            g1, g2 = _fan_gate(c, "intake_1"), _fan_gate(c, "intake_2")
            ev["intake_1_trip"] = {"cool": c, "intake_1": g1, "intake_2": g2}
            ctx.check(c.get("verdict") == "AIRFLOW", "an intake floor of %s rpm did not trip: %s", inl.get("hi"), c)
            ctx.check(c.get("fire_ok") is False and c.get("hold") is True,
                      "AIRFLOW without fire blocked and a hold: %s", c)
            ctx.check(g1.get("state") == "TRIPPED", "intake_1 does not read TRIPPED: %s", g1)
            ctx.check(g2.get("state") == "not fitted", "intake_2 was judged with one intake fan set: %s", g2)
            ctx.check("intake_1" in (c.get("reason") or ""), "the reason does not name intake_1: %r", c.get("reason"))
            c = _after_session(ctx, fc)
            ev["intake_1_trip_after"] = c
            ctx.check(c.get("verdict") == "OK" and c.get("hold") is False,
                      "the fan fault outlived its run session: %s", c)

            # Restore, and prove it: intake 2 judged again, every fan at or
            # above its floor. The restored grace is the shipped one.
            _set_gates(ctx, fc, orig)
            restored = True
            c = _run_session(ctx, grbl, fc,
                             lambda c: c.get("verdict") == "OK" and not c.get("gates_off")
                             and all(g.get("state") == "ok" for g in (c.get("fan_gates") or {}).values()),
                             "restored", wait=INTAKE_TRIP_WAIT_S + 20)
            ev["restored"] = c.get("fan_gates")
            ctx.check(c.get("verdict") == "OK" and c.get("gates_off") == [],
                      "engine did not return to OK with every gate on after the restore: %s", c)
            bad = {k: g for k, g in (c.get("fan_gates") or {}).items() if g.get("state") != "ok"}
            ctx.check(not bad, "fans not at or above their floors after the restore: %s", bad)
        finally:
            if not restored:
                st, body = fc.post("/settings", params=orig)
                ctx.log("restore on failure: POST /settings %s -> %s", orig, st)
                try:
                    c = _run_session(ctx, grbl, fc, lambda c: c.get("verdict") == "OK" and not c.get("gates_off"),
                                     "restore on failure")
                    ctx.log("restore on failure: engine %s gates_off %s", c.get("verdict"), c.get("gates_off"))
                except Exception as e:      # the original failure is the one to report
                    ctx.log("restore on failure: run session did not complete (%s)", e)
    after = fc.settings()
    ctx.check(all(after.get(k, "") == orig[k] for k in INTAKE_KEYS),
              "settings not restored: %s", {k: after.get(k) for k in INTAKE_KEYS})
    ctx.check(fc.wait_idle(60, abort=ctx.aborted), "machine did not return to idle")
