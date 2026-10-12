import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore

// BMeet panel widget: front end for bmeet-ctl
// (voxtype dictation + meetings, npu-transcribe files). State and backend calls live
// in Controller.qml; this file wires it to the Plasma applet.
//   left click   popup
//   middle click start / stop a meeting
PlasmoidItem {
    id: root

    Controller {
        id: vn
        expanded: root.expanded
        horizontal: Plasmoid.formFactor === PlasmaCore.Types.Horizontal
        vertical: Plasmoid.formFactor === PlasmaCore.Types.Vertical
        inTray: (Plasmoid.containmentDisplayHints & PlasmaCore.Types.ContainmentDrawsPlasmoidHeading) !== 0
        onExpandRequested: expand => root.expanded = expand
    }

    Plasmoid.icon: vn.iconName
    Plasmoid.title: "BMeet"
    Plasmoid.status: PlasmaCore.Types.ActiveStatus

    toolTipMainText: "BMeet: " + vn.stateText
                     + ((vn.meetingActive || vn.saving) && vn.elapsed > 0 ? " " + vn.elapsedText : "")
    toolTipSubText: {
        const lines = [];
        if (vn.statusError !== "")
            lines.push("Error: " + vn.statusError);
        if (vn.actionError !== "")
            lines.push("Error: " + vn.actionError);
        if (vn.meetingTitle !== "")
            lines.push(vn.meetingTitle);
        if (vn.engineText !== "")
            lines.push("Dictation: " + vn.engineText);
        lines.push("");
        lines.push("Meta+H dictate at cursor · Meta+Alt+H to clipboard");
        lines.push("Meta+Alt+M meeting · Meta+Shift+H switch language");
        lines.push("Middle-click: start/stop meeting");
        return lines.join("\n");
    }
    toolTipTextFormat: Text.PlainText

    compactRepresentation: CompactRepresentation {
        app: vn
    }

    fullRepresentation: FullRepresentation {
        app: vn
    }

    Plasmoid.contextualActions: [
        PlasmaCore.Action {
            text: vn.meetingActive ? "Stop meeting" : "Start meeting"
            icon.name: vn.meetingActive ? "media-playback-stop" : "media-record"
            enabled: vn.meetingActive ? !vn.saving : vn.canStartMeeting
            onTriggered: vn.toggleMeeting()
        },
        PlasmaCore.Action {
            text: "Transcribe a file…"
            icon.name: "document-open"
            onTriggered: vn.openApp("transcribe")
        },
        PlasmaCore.Action {
            text: "Open BMeet"
            icon.name: "window-new"
            onTriggered: vn.openApp("")
        }
    ]
}
