/*
 * help.js - forgetest: the page's help text
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 *
 * Every explanation the page offers lives here, keyed by the data-help
 * attribute of the "?" button that opens it (index.html): a title, its
 * paragraphs, and the page of the documentation site it deep-links to
 * (DOC_BASE + d). The buttons open Bootstrap popovers, one at a time; a
 * click anywhere else, Escape, or a tab change closes the open one. The
 * buttons sit on static elements only: the rows, prompts and tool entries
 * are rebuilt by app.js, and a popover on one of those would be orphaned.
 * What the operator must read mid-run (the steps, prompts, notices and
 * the live-laser acknowledgment) stays in the page and never moves here.
 */
var DOC_BASE = 'https://docs.forgefirm.org/';
var HELP = {
  campaign: {
    t: 'Campaign',
    d: 'developers/acceptance/',
    p: [
      'A release is authorized when a campaign is open on this image and every catalog test is satisfied: by a PASS in the campaign, or (never for the core) by an earlier PASS whose domain fingerprint is unchanged. A FAIL ends the campaign.',
      'The manifest identity is the image, the catalog hash is the test set; either changing starts the count over.'
    ]
  },
  queues: {
    t: 'Run what is left',
    d: 'developers/acceptance/',
    p: [
      'Each queue takes every test of its kind that the campaign does not already count as satisfied, runs them one at a time in prerequisite order, and stops on the first result that is not a PASS.',
      'The unattended queue needs nobody in the room; with the bench fixture up, the operator tests it can perform by itself run there too. The other one does: it prompts, and it fires the laser. Stop-the-queue cancels what is still waiting and lets the run in progress finish; Abort ends that one too.'
    ]
  },
  actions: {
    t: 'Campaign actions',
    d: 'developers/acceptance/',
    p: [
      'Export builds the release artifact the release gate reads (acceptance.json, with acceptance.md for people) from the campaign as it stands. The raw log is the append-only record; the journal is the daemon\'s own.',
      'Invalidate all forces a full campaign; give the reason. Reset campaign closes the open campaign without invalidating anything: earlier passes stay inheritable under the campaign rules, and the next Start opens a new one.'
    ]
  },
  ignore: {
    t: 'Ignore prerequisites',
    d: 'developers/acceptance/',
    p: [
      'A test\'s requires list only orders the runs. With the switch on, any test starts alone and its record notes which prerequisites were unmet; the release still needs every test satisfied.'
    ]
  },
  bench: {
    t: 'Bench diagnostics',
    d: 'developers/bench/',
    p: [
      'The bench tools under scripts/bench, run on the board with their output in the Run pane. Nothing here enters a campaign. Tools not yet ported are listed for completeness.',
      'Live tools need the operator acknowledgment; takeover and scope tools stop forgectrl for the duration and start it again when they finish.'
    ]
  }
};

var helpOpen = null;
function helpContent(h) {
  var d = document.createElement('div'),
    i,
    p,
    a;
  for (i = 0; i < h.p.length; i++) {
    p = document.createElement('p');
    p.textContent = h.p[i];
    d.appendChild(p);
  }
  a = document.createElement('a');
  a.className = 'doclink';
  a.href = DOC_BASE + h.d;
  a.target = '_blank';
  a.rel = 'noopener';
  a.textContent = 'Documentation \u2192';
  d.appendChild(a);
  return d;
}
function closeHelp() {
  if (helpOpen) {
    helpOpen.hide();
    helpOpen = null;
  }
}
function initHelp() {
  var els = document.querySelectorAll('[data-help]'),
    i;
  for (i = 0; i < els.length; i++) {
    (function (el) {
      var h = HELP[el.getAttribute('data-help')];
      if (!h) {
        el.style.display = 'none';
        return;
      }
      var pop = new bootstrap.Popover(el, {
        trigger: 'manual',
        html: true,
        placement: 'auto',
        title: h.t,
        content: helpContent(h),
        container: 'body'
      });
      el.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        if (helpOpen === pop) {
          closeHelp();
          return;
        }
        closeHelp();
        pop.show();
        helpOpen = pop;
      });
      el.addEventListener('hidden.bs.popover', function () {
        if (helpOpen === pop) helpOpen = null;
      });
    })(els[i]);
  }
  document.addEventListener('click', function (e) {
    var tip = document.querySelector('.popover.show');
    if (helpOpen && !(tip && tip.contains(e.target))) closeHelp();
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeHelp();
  });
}
