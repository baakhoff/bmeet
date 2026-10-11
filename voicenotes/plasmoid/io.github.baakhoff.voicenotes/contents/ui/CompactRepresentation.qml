import QtQuick
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.workspace.components as WorkspaceComponents

// Panel icon. Left click: popup. Middle click: start/stop a meeting.
// During a meeting the elapsed time is shown next to the icon in a horizontal
// panel, or as a small badge on the icon (vertical panel, system tray).
MouseArea {
    id: compact

    required property var app   // Controller

    readonly property bool showClock: app.meetingActive && !app.saving
    readonly property bool clockBeside: showClock && app.horizontal && !app.inTray
    readonly property bool clockBadge: showClock && !clockBeside && iconBox.width >= Kirigami.Units.iconSizes.smallMedium
    property bool wasExpanded: false

    hoverEnabled: true
    acceptedButtons: Qt.LeftButton | Qt.MiddleButton

    // Same sizing rules as Plasma's DefaultCompactRepresentation, plus room for the clock.
    Layout.minimumWidth: app.horizontal ? height + (clockBeside ? Kirigami.Units.smallSpacing + clockMetrics.clockWidth : 0)
                       : app.vertical ? 0 : Kirigami.Units.iconSizes.medium
    Layout.preferredWidth: Layout.minimumWidth
    Layout.maximumWidth: app.horizontal ? Layout.minimumWidth : Infinity
    Layout.minimumHeight: app.vertical ? width : app.horizontal ? 0 : Kirigami.Units.iconSizes.medium
    Layout.preferredHeight: Layout.minimumHeight
    Layout.maximumHeight: app.vertical ? Layout.minimumHeight : Infinity

    onPressed: mouse => {
        wasExpanded = app.expanded;
    }
    onClicked: mouse => {
        if (mouse.button === Qt.MiddleButton)
            app.toggleMeeting();
        else
            app.expandRequested(!wasExpanded);
    }

    Item {
        id: iconBox
        readonly property real side: Math.min(compact.height, compact.clockBeside ? compact.height : compact.width)
        width: side
        height: side
        x: compact.clockBeside ? 0 : Math.round((compact.width - side) / 2)
        y: Math.round((compact.height - side) / 2)

        Kirigami.Icon {
            id: icon
            anchors.fill: parent
            source: compact.app.iconName
            active: compact.containsMouse
            isMask: compact.app.iconTint !== ""
            color: compact.app.iconTint === "negative" ? Kirigami.Theme.negativeTextColor
                 : compact.app.iconTint === "neutral" ? Kirigami.Theme.neutralTextColor
                 : "transparent"
            opacity: 1
        }

        SequentialAnimation {
            running: compact.app.iconPulse && Kirigami.Units.longDuration > 0
            loops: Animation.Infinite
            onStopped: icon.opacity = 1
            NumberAnimation { target: icon; property: "opacity"; to: 0.35; duration: 700; easing.type: Easing.InOutQuad }
            NumberAnimation { target: icon; property: "opacity"; to: 1; duration: 700; easing.type: Easing.InOutQuad }
        }

        Loader {
            active: compact.clockBadge
            anchors.bottom: parent.bottom
            anchors.horizontalCenter: parent.horizontalCenter
            sourceComponent: WorkspaceComponents.BadgeOverlay {
                text: compact.app.elapsedText
                icon: iconBox
            }
        }
    }

    PlasmaComponents3.Label {
        id: clock
        visible: compact.clockBeside
        anchors.left: iconBox.right
        anchors.leftMargin: Kirigami.Units.smallSpacing
        anchors.verticalCenter: parent.verticalCenter
        width: clockMetrics.clockWidth
        text: compact.app.elapsedText
        textFormat: Text.PlainText
        font.features: { "tnum": 1 }
        opacity: compact.app.meetingPaused ? 0.6 : 1
    }

    // Fixed width so the panel does not jitter while the seconds tick.
    TextMetrics {
        id: clockMetrics
        font: clock.font
        text: compact.app.elapsed >= 3600 ? "0:00:00" : "00:00"
        readonly property real clockWidth: Math.ceil(advanceWidth) + 1
    }
}
