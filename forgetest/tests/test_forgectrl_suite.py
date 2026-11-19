# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""forgectrl.auth's proof that the running controller's own reports reach
the cooling engine, against a scripted daemon: the engine's report age read
a few report periods apart must stay fresh, which only the controller's
secret-bearing reports keep it. A controller whose reports the route
refuses leaves no report since its start (-1), or an age that grows."""
import unittest

import helpers
from forgetest import hw
from forgetest.runner import Failed
from forgetest.suite import forgectrl


class ReportsArrivingTests(unittest.TestCase):
    def setUp(self):
        self.fake = helpers.FakeForgectrl().start()
        self.addCleanup(self.fake.stop)
        self.ages = []

        def on_get(path, q):
            if path == "/cool/status":
                return 200, dict(self.fake.state["cool"], report_age_s=self.ages.pop(0))
            return None
        self.fake.on_get = on_get
        self.ctx = helpers.JudgeCtx(forgectrl=hw.Forgectrl())

    def test_the_controllers_reports_keep_the_engines_report_age_fresh(self):
        self.ages = [0.4, 0.7, 0.2]
        self.assertEqual(forgectrl.reports_arriving(self.ctx, self.ctx.forgectrl), [0.4, 0.7, 0.2])
        for refused in ([-1, -1, -1], [0.9, 2.9, 4.9]):         # none since the start; one that stopped
            self.ages = list(refused)
            with self.assertRaises(Failed):
                forgectrl.reports_arriving(self.ctx, self.ctx.forgectrl)


if __name__ == "__main__":
    unittest.main()
