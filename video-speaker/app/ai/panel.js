/* Video Speaker — AI assist panel (TEST FEATURE, safe to delete).

   This script builds its own UI, so index.html only needs the one <script>
   line. Delete the ai\ folder and that line 404s harmlessly — the tool behaves
   exactly as it did before.

   Nothing here changes the transcript on its own. Corrections are shown as a
   list and only applied when you tick them. The original text is kept and can
   be put back with one button. */

(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var box = function () { return $("t"); };          // the transcript textarea

  /* Transcripts made before the explanatory header was removed still have it
     cached. Never send it to the model — it is not speech, so "correcting" it
     would be meaningless. */
  function spoken(s) {
    return String(s || "").replace(/^\s*\[Rich transcript[\s\S]*?\]\s*\n?/, "").trim();
  }
  var models = [];
  var original = null;                                // text before any fix
  var lastFixes = [];

  /* ------------------------------------------------------------ styling -- */
  var css = document.createElement("link");
  css.rel = "stylesheet";
  css.href = "/ai/panel.css";
  document.head.appendChild(css);

  /* -------------------------------------------------------------- build -- */
  var wrap = document.createElement("div");
  wrap.className = "aip";
  wrap.innerHTML = [
    '<div class="aip-head">',
    '  <span class="aip-tag">AI</span>',
    '  <strong>Sharpen &amp; analyse</strong>',
    '  <span class="aip-note">test feature &middot; Ollama Cloud</span>',
    '  <button class="aip-cog" id="aiCog" title="Settings">&#9881;</button>',
    '</div>',

    '<div class="aip-setup" id="aiSetup">',
    '  <label>API key',
    '    <input id="aiKey" type="password" placeholder="paste from ollama.com/settings/keys"',
    '           autocomplete="off" spellcheck="false">',
    '  </label>',
    '  <div class="aip-keyrow">',
    '    <button id="aiSaveKey">Save key</button>',
    '    <span class="aip-hint" id="aiKeyState"></span>',
    '  </div>',
    '  <label>Model for reading and correcting text',
    '    <select id="aiModel"><option value="">loading…</option></select>',
    '  </label>',
    '  <label>Model for LOOKING at the video',
    '    <select id="aiVision"><option value="">loading…</option></select>',
    '  </label>',
    '  <p class="aip-small">Not every model can see pictures. Watching is almost'
    + ' all model time, so a fast one here is the difference between a wait and'
    + ' a long wait &mdash; minimax-m3 measured about 5x faster than gemma.</p>',
    '  <div class="aip-keyrow">',
    '    <input id="aiCustom" placeholder="…or type any model name (e.g. gemma4:31b-cloud)"',
    '           autocomplete="off" spellcheck="false">',
    '    <button id="aiUseCustom">Use it</button>',
    '  </div>',
    '  <div class="aip-keyrow">',
    '    <button id="aiTest">Test connection</button>',
    '    <span class="aip-hint" id="aiTestState"></span>',
    '  </div>',
    '  <p class="aip-small">The key is stored only on this PC, in',
    '     <code>D:\\VideoSpeaker\\ai\\config.json</code>. It is sent to',
    '     ollama.com and nowhere else. Set <code>OLLAMA_API_KEY</code> as an',
    '     environment variable instead and it is never written to disk at all.</p>',
    '</div>',

    '<div class="aip-actions">',
    '  <button class="aip-go" id="aiWatch">Watch the whole video &amp; check every word</button>',
    '  <select id="aiDetail" title="How often to look at a picture">',
    '    <option value="quick">quick look</option>',
    '    <option value="normal" selected>normal</option>',
    '    <option value="fine">frame by frame</option>',
    '  </select>',
    '  <button id="aiFindPrompt">Find the prompt on screen</button>',
    '  <button id="aiSharpen">Check every word</button>',
    '  <button id="aiAnalyze">Analyse in detail</button>',
    '  <button id="aiRestore" disabled>Undo AI changes</button>',
    '  <span class="aip-hint" id="aiState"></span>',
    '</div>',

    '<div class="aip-ask">',
    '  <input id="aiQuestion" placeholder="…or ask something specific about the video">',
    '  <button id="aiAsk">Ask</button>',
    '</div>',

    // the special window: whatever text was recovered off the screen
    '<div class="aip-promptbox" id="aiPromptBox" style="display:none">',
    '  <div class="aip-fixhead">',
    '    <strong>Prompt read off the screen</strong>',
    '    <span class="aip-small" id="aiPromptNote"></span>',
    '    <button id="aiPromptCopy">Copy</button>',
    '  </div>',
    '  <textarea id="aiPromptText" spellcheck="false"',
    '            placeholder="whatever was readable on screen will appear here"></textarea>',
    '  <div class="aip-small" id="aiPromptWhere"></div>',
    '</div>',

    '<div class="aip-out" id="aiSeen"></div>',
    '<div class="aip-out" id="aiFixes"></div>',
    '<div class="aip-out" id="aiAnswer"></div>'
  ].join("\n");

  /* Sit directly under the transcript box, where the copy buttons are — that is
     where you already are when you have a transcript in front of you. */
  var anchor = $("t");
  if (anchor && anchor.parentNode) {
    anchor.parentNode.insertBefore(wrap, anchor.nextSibling);
  } else {
    document.body.appendChild(wrap);
  }

  /* ------------------------------------------------------------- helpers -- */
  function state(el, msg, kind) {
    var e = $(el);
    e.textContent = msg || "";
    e.className = "aip-hint" + (kind ? " " + kind : "");
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function busy(on, msg) {
    ["aiSharpen", "aiAnalyze", "aiAsk"].forEach(function (id) {
      $(id).disabled = !!on;
    });
    state("aiState", msg || "", on ? "work" : "");
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return {}; })
        .then(function (j) {
          if (!r.ok) throw new Error(j.error || ("Request failed (" + r.status + ")"));
          return j;
        });
    });
  }

  /* The video's own title, channel and description are where most of the
     accuracy comes from — they usually spell out the names and jargon that
     speech-to-text mangles.

     The page keeps them in a `let lastInfo`, which is NOT reachable from another
     script (let does not attach to window), so this asks the server instead and
     remembers the answer. Doing it this way also means index.html needs no
     change beyond the single <script> line, which keeps removal clean. */
  var infoCache = { url: null, data: {} };

  function videoInfo() {
    var u = ($("url") && $("url").value || "").trim();
    if (!u) return Promise.resolve({});
    if (infoCache.url === u) return Promise.resolve(infoCache.data);

    return fetch("/api/info?url=" + encodeURIComponent(u))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        infoCache = {
          url: u,
          data: {
            title: d.title, uploader: d.uploader, channel: d.channel,
            site: d.site, description: d.description
          }
        };
        return infoCache.data;
      })
      .catch(function () { return {}; });     // no context is still workable
  }

  /* ------------------------------------------------------------- config --- */
  function loadConfig() {
    fetch("/api/ai/config").then(function (r) { return r.json(); })
      .then(function (c) {
        if (c.source === "saved") {
          state("aiKeyState",
                "saved on this PC " + (c.key_hint || "") + " — kept until you clear it", "ok");
        } else if (c.source === "env") {
          state("aiKeyState",
                "using OLLAMA_API_KEY " + (c.key_hint || "")
                + " — paste one above to use a different key", "ok");
        } else {
          state("aiKeyState", "no key yet", "warn");
          wrap.classList.add("open");        // nothing works without it
        }
        visionChosen = c.vision_model || "";
        fillVision();
        loadModels(c.model);
      })
      .catch(function () { state("aiKeyState", "could not read settings", "bad"); });
  }

  /* Ollama accepts "-cloud" aliases that its own /api/tags does NOT list, so a
     name can be perfectly valid and still be missing from the dropdown. Both
     gemma4:31b-cloud and gemma4:31b answer fine. Anything preferred or already
     saved is therefore added to the list rather than assumed to be in it —
     otherwise selecting it silently falls back to whatever is first. */
  var PREFERRED = ["gemma4:31b-cloud", "gemma4:31b", "gpt-oss:120b"];

  /* Verified 2026-07-27 to actually accept images, fastest first. gpt-oss:120b
     refuses outright ("does not support image input"), so it is deliberately
     not offered as eyes. */
  var VISION = ["minimax-m3", "minimax-m2.7", "minimax-m2.5",
                "gemma4:31b-cloud", "gemma4:31b"];
  var visionChosen = "";

  function fillVision() {
    var sel = $("aiVision");
    if (!sel) return;
    sel.innerHTML = VISION.map(function (m) {
      return '<option value="' + esc(m) + '">' + esc(m)
        + esc(m === VISION[0] ? "  — fastest" : "") + "</option>";
    }).join("");
    sel.value = (visionChosen && VISION.indexOf(visionChosen) >= 0)
      ? visionChosen : VISION[0];
    sel.onchange = function () {
      visionChosen = sel.value;
      post("/api/ai/config", { vision_model: sel.value }).catch(function () {});
    };
  }

  function loadModels(chosen) {
    fetch("/api/ai/models").then(function (r) { return r.json(); })
      .then(function (j) {
        var sel = $("aiModel");
        models = j.models || [];
        if (j.error && !models.length) {
          state("aiTestState", j.error, "bad");
        }

        var list = models.slice();
        // put anything valid-but-unlisted at the top, recommended first
        PREFERRED.concat(chosen ? [chosen] : []).slice().reverse()
          .forEach(function (m) {
            if (m && list.indexOf(m) < 0) list.unshift(m);
          });

        if (!list.length) {
          sel.innerHTML = '<option value="">could not load the list</option>';
          return;
        }

        sel.innerHTML = list.map(function (m) {
          var tag = (m === PREFERRED[0]) ? "  — recommended" : "";
          return '<option value="' + esc(m) + '">' + esc(m) + esc(tag) + "</option>";
        }).join("");

        var pick = "";
        if (chosen && list.indexOf(chosen) >= 0) pick = chosen;
        else if (!chosen) {
          for (var i = 0; i < PREFERRED.length; i++) {
            if (list.indexOf(PREFERRED[i]) >= 0) { pick = PREFERRED[i]; break; }
          }
        }
        if (!pick) pick = chosen || list[0];
        sel.value = pick;

        /* Only write a model back when there was NONE saved.

           This used to save whatever it had landed on every single time the
           panel opened. If ollama.com was slow or hiccuped, the list came back
           short, the saved model was not in it, and the recommended one was
           written over the top of the user's choice — so it "always started on
           the recommended one". A saved choice is now never overwritten by the
           panel merely loading; only by actually picking something. */
        if (!chosen && pick) post("/api/ai/config", { model: pick }).catch(function () {});
      })
      .catch(function () {
        $("aiModel").innerHTML = '<option value="">could not reach ollama.com</option>';
      });
  }

  function model() { return $("aiModel").value; }

  /* --------------------------------------------------------------- wire --- */
  $("aiCog").onclick = function () { wrap.classList.toggle("open"); };

  $("aiSaveKey").onclick = function () {
    var k = $("aiKey").value;
    post("/api/ai/config", { key: k }).then(function () {
      $("aiKey").value = "";
      // re-read, so the message states what is ACTUALLY in use now rather than
      // what was just typed — that gap is what made it look like nothing saved
      loadConfig();
    }).catch(function (e) { state("aiKeyState", e.message, "bad"); });
  };

  $("aiModel").onchange = function () {
    post("/api/ai/config", { model: model() }).catch(function () {});
  };

  /* Type a name Ollama accepts but does not advertise. It is added to the list
     and selected, so it behaves like any other choice from then on. */
  $("aiUseCustom").onclick = function () {
    var m = $("aiCustom").value.trim();
    if (!m) return state("aiTestState", "Type a model name first.", "warn");

    var sel = $("aiModel");
    if (!Array.prototype.some.call(sel.options, function (o) { return o.value === m; })) {
      var opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m;
      sel.insertBefore(opt, sel.firstChild);
    }
    sel.value = m;
    $("aiCustom").value = "";
    post("/api/ai/config", { model: m }).catch(function () {});
    state("aiTestState", "using " + m + " — press Test connection", "ok");
  };

  $("aiCustom").addEventListener("keydown", function (e) {
    if (e.key === "Enter") $("aiUseCustom").click();
  });

  $("aiTest").onclick = function () {
    state("aiTestState", "asking the model…", "work");
    post("/api/ai/test", { model: model() })
      .then(function (j) { state("aiTestState", "working — " + j.model, "ok"); })
      .catch(function (e) { state("aiTestState", e.message, "bad"); });
  };

  /* ------------------------------------------------------------ sharpen --- */
  $("aiSharpen").onclick = function () {
    var text = box() ? spoken(box().value) : "";
    if (!text) return state("aiState", "There is no transcript yet.", "bad");

    if (original === null) original = box().value;   // remember before touching
    busy(true, "checking every word against the video's own details…");
    $("aiFixes").innerHTML = "";

    videoInfo()
      .then(function (info) {
        return post("/api/ai/sharpen",
          { text: text, model: model(), info: info });
      })
      .then(function (j) {
        lastFixes = j.fixes || [];
        renderFixes(j);
        busy(false, lastFixes.length
          ? lastFixes.length + " possible correction" + (lastFixes.length === 1 ? "" : "s")
          : "Nothing looked mis-heard.");
      })
      .catch(function (e) { busy(false); state("aiState", e.message, "bad"); });
  };

  function renderFixes(j) {
    var host = $("aiFixes");
    if (!lastFixes.length) {
      host.innerHTML = '<p class="aip-small">No mis-hearings found. '
        + "The transcript is left exactly as it was.</p>";
      return;
    }
    host.innerHTML =
      '<div class="aip-fixhead">'
      + '<strong>' + lastFixes.length + ' suggested correction'
      + (lastFixes.length === 1 ? '' : 's') + '</strong>'
      + '<span class="aip-small">Nothing changes until you apply it.</span>'
      + '<button id="aiAll">Tick all high confidence</button>'
      + '<button class="aip-go" id="aiApply">Apply ticked</button>'
      + '</div>'
      + lastFixes.map(function (f, i) {
        return '<label class="aip-fix c-' + esc(f.confidence) + '">'
          + '<input type="checkbox" data-i="' + i + '">'
          + '<span class="aip-from">' + esc(f.from) + '</span>'
          + '<span class="aip-arrow">&rarr;</span>'
          + '<span class="aip-to">' + esc(f.to) + '</span>'
          + '<span class="aip-why">' + esc(f.why)
          + ' <em>(' + esc(f.confidence) + ')</em></span>'
          + '</label>';
      }).join("");

    $("aiAll").onclick = function () {
      host.querySelectorAll(".aip-fix.c-high input").forEach(function (c) {
        c.checked = true;
      });
    };
    $("aiApply").onclick = applyTicked;
  }

  function applyTicked() {
    var picked = [];
    $("aiFixes").querySelectorAll("input[type=checkbox]").forEach(function (c) {
      if (c.checked) picked.push(lastFixes[+c.dataset.i]);
    });
    if (!picked.length) return state("aiState", "Tick something first.", "warn");

    var text = box().value, done = 0;
    picked.forEach(function (f) {
      // only replace when the phrase is still uniquely findable, so two fixes
      // can never overlap and corrupt each other
      var parts = text.split(f.from);
      if (parts.length === 2) { text = parts.join(f.to); done++; }
    });
    box().value = text;
    $("aiRestore").disabled = false;
    state("aiState", "Applied " + done + " of " + picked.length
      + (done < picked.length ? " (the rest no longer matched)" : ""), "ok");
  }

  $("aiRestore").onclick = function () {
    if (original === null) return;
    box().value = original;
    $("aiRestore").disabled = true;
    state("aiState", "Put back the original transcript.", "ok");
  };

  /* ------------------------------------------------------------ analyse --- */
  function ask(question) {
    var text = box() ? spoken(box().value) : "";
    if (!text) return state("aiState", "There is no transcript yet.", "bad");

    busy(true, "reading the whole transcript…");
    $("aiAnswer").innerHTML = "";

    videoInfo()
      .then(function (info) {
        return post("/api/ai/analyze", {
          text: text, model: model(), info: info, question: question || ""
        });
      })
      .then(function (j) {
        $("aiAnswer").innerHTML =
          '<div class="aip-answer">' + esc(j.text) + "</div>"
          + '<button id="aiCopyAns">Copy this</button>';
        $("aiCopyAns").onclick = function () {
          navigator.clipboard.writeText(j.text)
            .then(function () { state("aiState", "Copied.", "ok"); })
            .catch(function () { state("aiState", "Could not copy.", "bad"); });
        };
        busy(false, "");
      })
      .catch(function (e) { busy(false); state("aiState", e.message, "bad"); });
  }

  /* ==================================================== watching the video ==
     This is the part that makes the AI worth having: without it the model only
     ever reads words and is blind to everything that happens on screen. */

  function secs(s) {                       // "1:23" or "83" -> 83
    s = String(s || "").trim();
    if (!s) return null;
    if (s.indexOf(":") < 0) return parseFloat(s) || 0;
    return s.split(":").reduce(function (a, p) { return a * 60 + (parseFloat(p) || 0); }, 0);
  }

  /* The green button watches the WHOLE video — not the part chosen on the seek
     bar — and then checks the transcript you already made against what it saw.
     The chosen range is for making the transcript; checking it has to cover
     everything, or a name shown at 2:40:00 never gets used. */
  $("aiWatch").onclick = function () {
    var url = ($("url") && $("url").value || "").trim();
    if (!url) return state("aiState", "Find a video first.", "bad");

    var text = box() ? spoken(box().value) : "";
    if (!text) {
      return state("aiState",
        "Get the transcript first — then this checks it against the video.", "bad");
    }

    state("aiState", "reading how long the video is…", "work");
    fetch("/api/info?url=" + encodeURIComponent(url))
      .then(function (r) { return r.json(); })
      .then(function (d) { start(d.duration || 0, d); })
      .catch(function () { state("aiState", "Could not read the video.", "bad"); });

    function start(duration, info) {
      if (!(duration > 0)) {
        return state("aiState", "Could not tell how long the video is.", "bad");
      }
      busy(true, "starting…");
      $("aiWatch").disabled = true;
      $("aiSeen").innerHTML = "";
      $("aiFixes").innerHTML = "";

      post("/api/ai/watch/start", {
        url: url, start: 0, end: duration,          // the ENTIRE video
        model: ($("aiVision") && $("aiVision").value) || "",   // eyes
        text_model: model(),                                   // for the checking
        detail: $("aiDetail").value,
        text: text,
        info: { title: info.title, uploader: info.uploader,
                site: info.site, description: info.description }
      }).then(function (j) {
        state("aiState", "watching the whole video — about " + j.planned
          + " moments, one every " + j.interval + "s", "work");
        poll(j.job);
      }).catch(function (e) {
        busy(false); $("aiWatch").disabled = false;
        state("aiState", e.message, "bad");
      });
    }

    function poll(jid) {
      var tick = setInterval(function () {
        fetch("/api/ai/watch/status?job=" + jid, { cache: "no-store" })
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (j.error && !j.state) { throw new Error(j.error); }

            if (j.state === "error") {
              clearInterval(tick);
              busy(false); $("aiWatch").disabled = false;
              state("aiState", j.error || "It could not finish.", "bad");
              return;
            }

            var msg = j.message || "working…";
            if (j.total) msg += "   (" + Math.round(100 * (j.done || 0) / j.total) + "%)";
            state("aiState", msg, "work");

            if (j.state === "done") {
              clearInterval(tick);
              renderSeen({ frames: j.seen || [], count: j.count || 0,
                           interval: j.interval });
              lastFixes = j.fixes || [];
              renderFixes({ fixes: lastFixes });
              autoTickHigh();
              busy(false, "Watched the whole video — " + (j.count || 0)
                + " moments, " + lastFixes.length + " thing"
                + (lastFixes.length === 1 ? "" : "s") + " to check");
              $("aiWatch").disabled = false;
            }
          })
          .catch(function (e) {
            clearInterval(tick);
            busy(false); $("aiWatch").disabled = false;
            state("aiState", e.message, "bad");
          });
      }, 1500);
    }
  };

  /* ============================================ reading a prompt off screen ==
     For tutorial videos where someone shows a prompt but never shares it. The
     text is rarely fully visible in one frame, so this reads many frames and
     stitches the overlapping pieces together. */
  $("aiFindPrompt").onclick = function () {
    var url = ($("url") && $("url").value || "").trim();
    if (!url) return state("aiState", "Find a video first.", "bad");

    state("aiState", "reading how long the video is…", "work");
    fetch("/api/info?url=" + encodeURIComponent(url))
      .then(function (r) { return r.json(); })
      .then(function (d) { go(d.duration || 0); })
      .catch(function () { state("aiState", "Could not read the video.", "bad"); });

    function go(duration) {
      if (!(duration > 0)) {
        return state("aiState", "Could not tell how long the video is.", "bad");
      }
      busy(true, "starting…");
      $("aiFindPrompt").disabled = true;
      $("aiPromptBox").style.display = "none";

      post("/api/ai/prompt/start", {
        url: url, start: 0, end: duration,
        model: ($("aiVision") && $("aiVision").value) || "",
        text_model: model(),
        // the spoken words help settle a word that is blurry on screen
        text: box() ? spoken(box().value) : ""
      }).then(function (j) { poll(j.job); })
        .catch(function (e) {
          busy(false); $("aiFindPrompt").disabled = false;
          state("aiState", e.message, "bad");
        });
    }

    function poll(jid) {
      var tick = setInterval(function () {
        fetch("/api/ai/watch/status?job=" + jid, { cache: "no-store" })
          .then(function (r) { return r.json(); })
          .then(function (j) {
            if (j.error && !j.state) throw new Error(j.error);
            if (j.state === "error") {
              clearInterval(tick);
              busy(false); $("aiFindPrompt").disabled = false;
              state("aiState", j.error || "It could not finish.", "bad");
              return;
            }
            var msg = j.message || "working…";
            if (j.total) msg += "   (" + Math.round(100 * (j.done || 0) / j.total) + "%)";
            state("aiState", msg, "work");

            if (j.state === "done") {
              clearInterval(tick);
              showPrompt(j);
              busy(false, j.message || "");
              $("aiFindPrompt").disabled = false;
            }
          })
          .catch(function (e) {
            clearInterval(tick);
            busy(false); $("aiFindPrompt").disabled = false;
            state("aiState", e.message, "bad");
          });
      }, 1500);
    }
  };

  function showPrompt(j) {
    var frags = j.fragments || [];
    if (!j.prompt) {
      $("aiPromptBox").style.display = "none";
      state("aiState", j.note || "Nothing readable was on screen.", "warn");
      return;
    }
    $("aiPromptBox").style.display = "";
    $("aiPromptText").value = j.prompt;

    /* Say plainly how complete it is. A prompt that is quietly missing its
       middle is worse than one that admits the gap. */
    var label = { yes: "looks complete", partly: "partly readable — gaps marked [...]",
                  no: "incomplete" }[j.complete] || "";
    $("aiPromptNote").textContent = label + (j.note ? " · " + j.note : "");

    $("aiPromptWhere").textContent = frags.length
      ? "Read from " + frags.length + " moment" + (frags.length === 1 ? "" : "s")
        + ": " + frags.slice(0, 12).map(function (f) { return f.at; }).join(", ")
        + (frags.length > 12 ? " …" : "")
      : "";

    $("aiPromptCopy").onclick = function () {
      navigator.clipboard.writeText($("aiPromptText").value)
        .then(function () { state("aiState", "Copied the prompt.", "ok"); })
        .catch(function () { state("aiState", "Could not copy.", "bad"); });
    };
  }

  /* The confident corrections are ticked already, so getting it right is one
     click — but you can still see and untick every one of them first. */
  function autoTickHigh() {
    var boxes = $("aiFixes").querySelectorAll(".aip-fix.c-high input");
    for (var i = 0; i < boxes.length; i++) boxes[i].checked = true;
  }

  var seenFrames = [];

  function renderSeen(j) {
    seenFrames = j.frames || [];
    if (!seenFrames.length) {
      $("aiSeen").innerHTML = '<p class="aip-small">Nothing could be seen in that part.</p>';
      buildSeenBlock();
      return;
    }
    $("aiSeen").innerHTML =
      '<div class="aip-fixhead"><strong>What is on screen</strong>'
      + '<span class="aip-small">from the pictures, not the words</span>'
      + '<button id="aiCopySeen">Copy this</button></div>'
      + seenFrames.map(function (f) {
        return '<div class="aip-seen"><span class="aip-at">' + esc(f.at) + "</span>"
          + "<span>" + esc(f.seen) + "</span></div>";
      }).join("");

    $("aiCopySeen").onclick = function () {
      navigator.clipboard.writeText(seenText())
        .then(function () { state("aiState", "Copied.", "ok"); })
        .catch(function () { state("aiState", "Could not copy.", "bad"); });
    };
    buildSeenBlock();
  }

  function seenText() {
    return seenFrames.map(function (f) { return "[" + f.at + "] " + f.seen; }).join("\n");
  }

  /* Hand the result to the page's "Copy as prompt" button.

     It is passed as a ready-made string on window, and index.html only adds it
     IF it is there. That keeps the whole thing removable: delete the ai\ folder
     and the variable never exists, so the button behaves exactly as before. */
  function buildSeenBlock() {
    if (!seenFrames.length) { window.__videoSeenBlock = ""; return; }

    var spokenText = box() ? spoken(box().value) : "";
    var timed = /^\[\d+:\d\d\]/m.test(spokenText);

    if (!timed) {
      // no timestamps to line up with — keep the two apart rather than guess
      window.__videoSeenBlock =
        "\n=== WHAT IS ON SCREEN (from the pictures) ===\n"
        + seenText() + "\n=== END ON SCREEN ===\n";
      return;
    }

    // Merge into one timeline: at each moment, what was seen and what was said.
    var said = [];
    spokenText.split("\n").forEach(function (line) {
      var m = line.match(/^\[(\d+):(\d\d)\]\s*(.*)$/);
      if (m) said.push({ t: (+m[1]) * 60 + (+m[2]), text: m[3] });
    });

    var rows = seenFrames.map(function (f) {
      return { t: hms(f.at), at: f.at, seen: f.seen, said: [] };
    });
    said.forEach(function (s) {
      var best = null;
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].t <= s.t) best = rows[i]; else break;
      }
      (best || rows[0]).said.push(s.text);
    });

    var out = rows.map(function (r) {
      var block = "[" + r.at + "] SEEN: " + r.seen;
      if (r.said.length) block += "\n        SAID: " + r.said.join(" ");
      return block;
    }).join("\n");

    window.__videoSeenBlock =
      "\n=== WHAT HAPPENS, SEEN AND HEARD TOGETHER ===\n"
      + out + "\n=== END ===\n";
  }

  function hms(at) {
    var p = String(at).split(":");
    return (+p[0]) * 60 + (+p[1] || 0);
  }

  $("aiAnalyze").onclick = function () { ask(""); };
  $("aiAsk").onclick = function () { ask($("aiQuestion").value.trim()); };
  $("aiQuestion").addEventListener("keydown", function (e) {
    if (e.key === "Enter") $("aiAsk").click();
  });

  loadConfig();
})();
