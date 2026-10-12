import QtQuick
import org.kde.plasma.plasma5support as P5Support

// State and backend calls of the BMeet widget. Everything goes through
// bmeet-ctl (JSON on stdout). Kept free of Plasmoid.* so both representations
// can also be loaded outside plasmashell for testing: main.qml feeds in
// expanded/horizontal/vertical/inTray and listens to expandRequested().
Item {
    id: vn
    visible: false

    // ---- inputs ------------------------------------------------------------
    property bool expanded: false
    property bool horizontal: true
    property bool vertical: false
    property bool inTray: false
    property bool polling: true      // false = no backend calls at all (tests inject state)
    // Runs via /bin/sh -c, so $HOME is expanded by the shell. The installer links
    // bmeet-ctl here; ~/.local/bin is not always in plasmashell's PATH.
    property string ctl: '"$HOME/.local/bin/bmeet-ctl"'

    signal expandRequested(bool expand)

    // ---- backend state (bmeet-ctl status) ------------------------------
    property bool haveStatus: false
    property string daemon: ""
    property string dictation: "off"
    property string meeting: "idle"
    property string meetingId: ""
    property string meetingTitle: ""
    property real meetingStarted: 0
    property string language: ""
    property string nextLanguage: ""     // what "lang toggle" switches to ("" = only one configured)
    property string model: ""
    property string device: ""
    property string statusError: ""

    // ---- bmeet-ctl recent ----------------------------------------------
    property var recentItems: []
    property bool recentLoaded: false
    property string recentError: ""

    // Last failed user action; shown in the popup and the tooltip until dismissed.
    property string actionError: ""

    // ---- widget-side transitions --------------------------------------------
    property real now: Date.now() / 1000
    property bool starting: false        // Start pressed, meeting not running yet
    property real startingSince: 0
    property bool saving: false          // Stop pressed, meeting state not idle yet
    property real savingSince: 0
    property int frozenElapsed: 0
    property bool langSwitching: false   // lang toggle sent, daemon restarting
    property real langSince: 0
    property string langFrom: ""
    property string langTo: ""
    // The backend has no "active seconds", only the start time, so pauses are
    // tracked here (only those that happen while the widget is loaded).
    property string clockMeeting: ""
    property real pausedAt: 0
    property real pausedTotal: 0

    // ---- derived ------------------------------------------------------------
    readonly property bool dictRecording: dictation === "recording" || dictation === "streaming"
    readonly property bool dictTranscribing: dictation === "transcribing"
    readonly property bool dictBusy: dictRecording || dictTranscribing
    readonly property bool meetingRecording: meeting === "recording"
    readonly property bool meetingPaused: meeting === "paused"
    readonly property bool meetingActive: meetingRecording || meetingPaused
    // Unknown non-idle meeting states are treated as "finishing" as well.
    readonly property bool meetingFinishing: saving || (meeting !== "idle" && !meetingActive)
    readonly property bool daemonOff: haveStatus && daemon !== "active" && !langSwitching
    readonly property bool anyActive: dictBusy || meeting !== "idle" || saving || starting || langSwitching
    readonly property bool canStartMeeting: haveStatus && !daemonOff && meeting === "idle" && !saving && !starting && !langSwitching
    readonly property bool canSwitchLanguage: haveStatus && meeting === "idle" && !saving && !starting
                                              && !dictBusy && !langSwitching && nextLanguage !== ""
    readonly property string languageCode: (language || "?").toUpperCase()
    readonly property string otherLanguageCode: nextLanguage.toUpperCase()

    readonly property int elapsed: {
        if (saving)
            return frozenElapsed;
        if (!meetingActive || meetingStarted <= 0)
            return 0;
        const end = pausedAt > 0 ? pausedAt : now;
        return Math.max(0, Math.floor(end - meetingStarted - pausedTotal));
    }
    readonly property string elapsedText: formatClock(elapsed)

    // One word for the overall state; drives icon, texts and colours.
    readonly property string mode: {
        if (statusError !== "")
            return "error";
        if (!haveStatus)
            return "loading";
        if (langSwitching)
            return "switching";
        if (daemonOff)
            return "off";
        if (meetingFinishing)
            return "saving";
        if (meetingRecording)
            return "meeting";
        if (meetingPaused)
            return "paused";
        if (starting)
            return "starting";
        if (dictRecording)
            return "dictating";
        if (dictTranscribing)
            return "transcribing";
        return "idle";
    }

    // Monochrome "-symbolic" mic so it matches other panel icons and can be tinted
    // (Kirigami recolours only SVGs that use the ColorScheme-* classes).
    readonly property string iconName: ({
        "error": "dialog-warning",
        "loading": "audio-input-microphone-symbolic",
        "switching": "preferences-desktop-locale",
        "off": "mic-off",
        "saving": "document-save",
        "meeting": "media-record",
        "paused": "media-playback-pause",
        "starting": "media-record",
        "dictating": "audio-input-microphone-symbolic",
        "transcribing": "audio-input-microphone-symbolic",
        "idle": "audio-input-microphone-symbolic"
    })[mode] || "audio-input-microphone-symbolic"
    // "" = icon as is, otherwise the representations tint it with Kirigami.Theme.<x>TextColor.
    readonly property string iconTint: mode === "dictating" ? "negative" : mode === "transcribing" ? "neutral" : ""
    readonly property bool iconPulse: ["switching", "saving", "starting", "transcribing", "loading"].includes(mode)

    readonly property string stateText: ({
        "error": "Backend error",
        "loading": "Checking…",
        "switching": "Switching language to " + otherLanguageCodeFrom + "…",
        "off": "Dictation service not running",
        "saving": "Saving transcript…",
        "meeting": "Recording meeting",
        "paused": "Meeting paused",
        "starting": "Starting meeting…",
        "dictating": "Dictating…",
        "transcribing": "Transcribing dictation…",
        "idle": "Ready"
    })[mode] || mode
    readonly property string otherLanguageCodeFrom: langTo.toUpperCase()

    readonly property string engineText: [languageCode !== "?" ? languageCode : "", model, device]
        .filter(s => s).join(" · ")

    // ---- process plumbing ---------------------------------------------------
    property int seq: 0
    property var pending: ({})
    property bool statusBusy: false
    property int statusFailures: 0
    property real statusSince: 0

    P5Support.DataSource {
        id: exec
        engine: "executable"
        connectedSources: []
        onNewData: (source, data) => {
            exec.disconnectSource(source);
            const cb = vn.pending[source];
            delete vn.pending[source];
            if (cb)
                cb(Number(data["exit code"]), data["stdout"] || "", data["stderr"] || "");
        }
    }

    // Every source string must be unique: the engine ignores a connectSource() for a
    // source that is still connected and may hand back cached output. " #..." is a
    // shell comment (the engine runs the string with /bin/sh -c).
    function run(args, cb) {
        if (!vn.polling) {
            console.log("bmeet: not running (polling off):", args);
            return;
        }
        vn.seq += 1;
        const source = vn.ctl + " " + args + " #" + Date.now() + "." + vn.seq;
        vn.pending[source] = cb || null;
        exec.connectSource(source);
    }

    function shq(s) {
        return "'" + String(s).replace(/'/g, "'\\''") + "'";
    }

    function failText(code, stdout, stderr) {
        const pick = s => {
            const lines = (s || "").trim().split("\n").filter(l => l.trim() !== "");
            return lines.length ? lines[lines.length - 1].trim() : "";
        };
        return pick(stderr) || pick(stdout) || ("exit code " + code);
    }

    // ---- status -------------------------------------------------------------
    function refreshStatus() {
        if (!vn.polling)
            return;
        const t = Date.now();
        if (vn.statusBusy && t - vn.statusSince < 10000)
            return;
        vn.statusBusy = true;
        vn.statusSince = t;
        vn.run("status", (code, out, err) => {
            vn.statusBusy = false;
            let s = null;
            let problem = "";
            if (code !== 0) {
                problem = vn.failText(code, out, err);
            } else {
                try {
                    s = JSON.parse(out);
                } catch (e) {
                    problem = "unreadable output (" + e.message + ")";
                }
            }
            if (problem !== "") {
                // One failed poll (e.g. the meeting DB busy for 2 s) is not worth a
                // warning icon; two in a row, or no status yet, is.
                vn.statusFailures += 1;
                if (vn.statusFailures >= 2 || !vn.haveStatus)
                    vn.statusError = "bmeet-ctl status: " + problem;
                return;
            }
            vn.statusFailures = 0;
            vn.statusError = "";
            vn.applyStatus(s);
        });
    }

    function applyStatus(s) {
        const wasMeeting = vn.meeting;
        const t = Date.now() / 1000;
        vn.now = t;
        vn.daemon = s.daemon || "";
        vn.dictation = s.dictation || "off";
        vn.meeting = s.meeting || "idle";
        vn.meetingId = s.meeting_id || "";
        vn.meetingTitle = s.meeting_title || "";
        vn.meetingStarted = Number(s.meeting_started) || 0;
        vn.language = s.language || "";
        vn.nextLanguage = s.next_language || "";
        vn.model = s.model || "";
        vn.device = s.device || "";
        vn.haveStatus = true;

        // pause bookkeeping for the elapsed clock
        if (vn.meetingId !== vn.clockMeeting) {
            vn.clockMeeting = vn.meetingId;
            vn.pausedAt = 0;
            vn.pausedTotal = 0;
        }
        if (vn.meeting === "paused" && vn.pausedAt === 0) {
            vn.pausedAt = t;
        } else if (vn.meeting === "recording" && vn.pausedAt > 0) {
            vn.pausedTotal += t - vn.pausedAt;
            vn.pausedAt = 0;
        }

        if (vn.starting) {
            if (vn.meeting !== "idle") {
                vn.starting = false;
            } else if (t - vn.startingSince > 20) {
                vn.starting = false;
                vn.actionError = "The meeting did not start - see the notification (is voxtype running?).";
            }
        }
        if (vn.saving) {
            if (vn.meeting === "idle") {
                vn.saving = false;
            } else if (t - vn.savingSince > 330) {
                vn.saving = false;
                vn.actionError = "Meeting still not finished 5 min after Stop - see the notifications or run: voxtype meeting status";
            }
        }
        if (vn.langSwitching) {
            if (vn.language !== vn.langFrom && vn.daemon === "active" && t - vn.langSince >= 4) {
                vn.langSwitching = false;
            } else if (t - vn.langSince > 25) {
                vn.langSwitching = false;
                if (vn.language === vn.langFrom)
                    vn.actionError = "Language not changed - see the notification.";
            }
        }
        // A meeting just ended (from here or via Meta+Alt+M): its entry appears in
        // `recent` now; the Markdown export follows a moment later.
        if (wasMeeting !== "idle" && vn.meeting === "idle") {
            vn.refreshRecent();
            recentAgain.restart();
        }
    }

    // ---- recent -------------------------------------------------------------
    function refreshRecent() {
        if (!vn.polling)
            return;
        vn.run("recent --limit 5", (code, out, err) => {
            if (code !== 0) {
                vn.recentError = vn.failText(code, out, err);
                return;
            }
            try {
                const list = JSON.parse(out);
                vn.recentItems = Array.isArray(list) ? list : [];
                vn.recentError = "";
                vn.recentLoaded = true;
            } catch (e) {
                vn.recentError = "unreadable output (" + e.message + ")";
            }
        });
    }

    // ---- actions ------------------------------------------------------------
    // Generic: run, report a non-zero exit as actionError, re-poll status.
    function act(args, label, after) {
        vn.actionError = "";
        vn.run(args, (code, out, err) => {
            if (code !== 0)
                vn.actionError = label + " failed: " + vn.failText(code, out, err);
            if (after)
                after(code === 0);
            vn.refreshStatus();
        });
    }

    // Toggle; always to the clipboard (the popup has focus, so "paste at cursor"
    // would land in the popup).
    function dictateToClipboard() {
        vn.act("dictate --clipboard", "Dictation");
    }

    function cancelDictation() {
        vn.act("cancel", "Cancel");
    }

    function toggleLanguage() {
        if (!vn.canSwitchLanguage)
            return;
        vn.langFrom = vn.language;
        vn.langTo = vn.nextLanguage;
        vn.langSince = Date.now() / 1000;
        vn.langSwitching = true;
        vn.act("lang " + vn.nextLanguage, "Language switch", ok => {
            if (!ok)
                vn.langSwitching = false;
        });
    }

    function startMeeting() {
        if (!vn.canStartMeeting)
            return;
        vn.starting = true;
        vn.startingSince = Date.now() / 1000;
        vn.act("meeting start", "Meeting start", ok => {
            if (!ok)
                vn.starting = false;
        });
    }

    function stopMeeting() {
        if (!vn.meetingActive || vn.saving)
            return;
        vn.frozenElapsed = vn.elapsed;
        vn.saving = true;
        vn.savingSince = Date.now() / 1000;
        vn.act("meeting stop", "Meeting stop", ok => {
            if (!ok)
                vn.saving = false;
        });
    }

    function pauseMeeting() {
        vn.act("meeting pause", "Pause");
    }

    function resumeMeeting() {
        vn.act("meeting resume", "Resume");
    }

    // Middle click / context menu: start when idle, stop when running.
    function toggleMeeting() {
        if (vn.meetingActive)
            vn.stopMeeting();
        else
            vn.startMeeting();
    }

    function openApp(args) {
        vn.act("open" + (args ? " " + args : ""), "Opening BMeet", ok => {
            if (ok)
                vn.expandRequested(false);
        });
    }

    function openRecent(item) {
        if (item && item.kind === "meeting" && item.id)
            vn.openApp("meetings --id " + vn.shq(item.id));
        else
            vn.openApp("files");
    }

    // ---- formatting ---------------------------------------------------------
    function pad2(n) {
        return (n < 10 ? "0" : "") + n;
    }

    // 754 -> "12:34", 3754 -> "1:02:34"
    function formatClock(secs) {
        secs = Math.max(0, Math.floor(Number(secs) || 0));
        const h = Math.floor(secs / 3600);
        const m = Math.floor(secs % 3600 / 60);
        const s = secs % 60;
        return h > 0 ? h + ":" + pad2(m) + ":" + pad2(s) : pad2(m) + ":" + pad2(s);
    }

    // Recording length for the list: "45 s", "12 min", "1 h 05 min"
    function formatLength(secs) {
        const n = Number(secs);
        if (secs === null || secs === undefined || secs === "" || isNaN(n) || n <= 0)
            return "";
        if (n < 60)
            return Math.round(n) + " s";
        if (n < 3600)
            return Math.round(n / 60) + " min";
        return Math.floor(n / 3600) + " h " + pad2(Math.round(n % 3600 / 60)) + " min";
    }

    // epoch seconds | epoch ms | numeric string | ISO string -> epoch seconds (0 = unknown)
    function toEpoch(t) {
        if (t === null || t === undefined || t === "")
            return 0;
        const n = Number(t);
        if (!isNaN(n))
            return n > 1e12 ? n / 1000 : n;
        const ms = Date.parse(t);
        return isNaN(ms) ? 0 : ms / 1000;
    }

    function relativeTime(t) {
        const e = toEpoch(t);
        if (!e)
            return "";
        const d = vn.now - e;
        if (d < 45)
            return "just now";
        if (d < 3600)
            return Math.max(1, Math.round(d / 60)) + " min ago";
        const when = new Date(e * 1000);
        const today = new Date(vn.now * 1000);
        const hm = Qt.formatTime(when, Qt.locale().timeFormat(Locale.ShortFormat));
        if (when.toDateString() === today.toDateString())
            return "today " + hm;
        const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
        if (when.toDateString() === yesterday.toDateString())
            return "yesterday " + hm;
        if (d < 6 * 86400)
            return Qt.formatDate(when, "ddd") + " " + hm;
        return Qt.formatDate(when, when.getFullYear() === today.getFullYear() ? "d MMM" : "d MMM yyyy");
    }

    // ---- timers -------------------------------------------------------------
    // ~1 s while the popup is open or anything is going on, otherwise every 4 s.
    Timer {
        interval: (vn.expanded || vn.anyActive) ? 1000 : 4000
        running: vn.polling
        repeat: true
        triggeredOnStart: true
        onTriggered: vn.refreshStatus()
    }

    // Smooth meeting clock between status replies.
    Timer {
        interval: 1000
        running: vn.meetingRecording && !vn.saving
        repeat: true
        onTriggered: vn.now = Date.now() / 1000
    }

    Timer {
        id: recentAgain
        interval: 5000
        onTriggered: vn.refreshRecent()
    }

    onExpandedChanged: {
        if (expanded) {
            refreshStatus();
            refreshRecent();
        }
    }
}
