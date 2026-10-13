import QtQuick
import QtQuick.Layouts
import QtQuick.Templates as T
import org.kde.kirigami as Kirigami
import org.kde.plasma.components as PlasmaComponents3
import org.kde.plasma.extras as PlasmaExtras

// Popup: state + language, dictation, meeting, recent items, app shortcuts.
PlasmaExtras.Representation {
    id: full

    required property var app   // Controller

    readonly property color tintColor: app.iconTint === "negative" ? Kirigami.Theme.negativeTextColor
                                     : app.iconTint === "neutral" ? Kirigami.Theme.neutralTextColor
                                     : "transparent"

    Layout.minimumWidth: Kirigami.Units.gridUnit * 18
    Layout.preferredWidth: Kirigami.Units.gridUnit * 22
    Layout.minimumHeight: Kirigami.Units.gridUnit * 12
    Layout.preferredHeight: (header ? header.implicitHeight : 0) + content.implicitHeight
                            + topPadding + bottomPadding + (footer ? footer.implicitHeight : 0)

    header: PlasmaExtras.PlasmoidHeading {
        contentItem: RowLayout {
            spacing: Kirigami.Units.smallSpacing

            Kirigami.Icon {
                Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                Layout.preferredHeight: Kirigami.Units.iconSizes.medium
                source: full.app.iconName
                isMask: full.app.iconTint !== ""
                color: full.tintColor
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0

                PlasmaExtras.Heading {
                    Layout.fillWidth: true
                    level: 3
                    text: full.app.stateText
                          + ((full.app.meetingActive || full.app.saving) && full.app.elapsed > 0 ? "  " + full.app.elapsedText : "")
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                }

                PlasmaComponents3.Label {
                    Layout.fillWidth: true
                    visible: text !== ""
                    text: full.app.statusError !== "" ? full.app.statusError : full.app.engineText
                    textFormat: Text.PlainText
                    font: Kirigami.Theme.smallFont
                    color: full.app.statusError !== "" ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
                    opacity: full.app.statusError !== "" ? 1 : 0.7
                    wrapMode: full.app.statusError !== "" ? Text.Wrap : Text.NoWrap
                    elide: Text.ElideRight
                    maximumLineCount: 3
                }
            }

            // Language "chip": click switches to the next configured language (restarts the dictation daemon). Wrapped so
            // the tooltip can explain why it is disabled (disabled items get no hover).
            Item {
                visible: full.app.language !== ""
                implicitWidth: langChip.implicitWidth
                implicitHeight: langChip.implicitHeight

                HoverHandler {
                    id: chipHover
                }

                PlasmaComponents3.ToolButton {
                    id: langChip
                    anchors.fill: parent
                    flat: false
                    text: full.app.langSwitching ? full.app.otherLanguageCodeFrom + "…" : full.app.languageCode
                    icon.name: "preferences-desktop-locale"
                    enabled: full.app.canSwitchLanguage
                    onClicked: full.app.toggleLanguage()
                    Accessible.name: "Dictation language " + full.app.languageCode
                    Accessible.description: chipTip.text
                }

                PlasmaComponents3.ToolTip {
                    id: chipTip
                    parent: langChip
                    visible: chipHover.hovered
                    delay: Kirigami.Units.toolTipDelay
                    text: full.app.langSwitching ? "Restarting the dictation engine…"
                        : full.app.meeting !== "idle" || full.app.saving || full.app.starting
                            ? "The language cannot change during a meeting (the engine restart would cut it short)."
                        : full.app.dictBusy ? "The language cannot change while dictating."
                        : full.app.nextLanguage === "" ? "Dictation language: " + full.app.languageCode
                          + ". Add more in ~/.config/bmeet/languages to switch here."
                        : "Dictation language: " + full.app.languageCode + ". Click to switch to "
                          + full.app.otherLanguageCode + " (restarts the engine, ~3 s). Shortcut: Meta+Shift+H"
                }
            }
        }
    }

    contentItem: ColumnLayout {
        id: content
        spacing: Kirigami.Units.smallSpacing

        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: full.app.actionError !== ""
            type: Kirigami.MessageType.Error
            text: full.app.actionError
            actions: [
                Kirigami.Action {
                    icon.name: "dialog-close"
                    text: "Dismiss"
                    onTriggered: full.app.actionError = ""
                }
            ]
        }

        Kirigami.InlineMessage {
            Layout.fillWidth: true
            visible: full.app.mode === "off"
            type: Kirigami.MessageType.Warning
            text: "The voxtype dictation service is not running, so dictation and meetings are unavailable.\n"
                  + "Check: systemctl --user status voxtype"
        }

        // ---- Dictation ------------------------------------------------------
        PlasmaExtras.Heading {
            Layout.fillWidth: true
            level: 4
            text: "Dictation"
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing

            PlasmaComponents3.Button {
                icon.name: full.app.dictRecording ? "media-playback-stop" : "edit-paste"
                text: full.app.dictRecording ? "Stop and copy"
                    : full.app.dictTranscribing ? "Transcribing…"
                    : "Dictate to clipboard"
                enabled: full.app.haveStatus && !full.app.daemonOff && !full.app.dictTranscribing && !full.app.langSwitching
                onClicked: full.app.dictateToClipboard()
            }

            PlasmaComponents3.BusyIndicator {
                visible: full.app.dictTranscribing
                Layout.preferredWidth: Kirigami.Units.iconSizes.smallMedium
                Layout.preferredHeight: Kirigami.Units.iconSizes.smallMedium
            }

            PlasmaComponents3.Button {
                visible: full.app.dictBusy
                icon.name: "dialog-cancel"
                text: "Cancel"
                onClicked: full.app.cancelDictation()
            }

            Item {
                Layout.fillWidth: true
            }
        }

        PlasmaExtras.DescriptiveLabel {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            font: Kirigami.Theme.smallFont
            text: full.app.dictRecording
                  ? "Speak now; click Stop and copy when done, then paste with Ctrl+V."
                  : "To type straight into a window, focus it and press Meta+H (again to stop). "
                    + "Meta+Alt+H dictates to the clipboard from anywhere."
        }

        // ---- Meeting --------------------------------------------------------
        PlasmaExtras.Heading {
            Layout.fillWidth: true
            Layout.topMargin: Kirigami.Units.smallSpacing
            level: 4
            text: "Meeting"
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing

            PlasmaComponents3.Button {
                visible: full.app.meeting === "idle" && !full.app.saving
                icon.name: "media-record"
                text: full.app.starting ? "Starting…" : "Start meeting"
                enabled: full.app.canStartMeeting
                onClicked: full.app.startMeeting(false)
            }

            PlasmaComponents3.Button {
                id: inPersonButton
                visible: full.app.meeting === "idle" && !full.app.saving && !full.app.starting
                icon.name: "group"
                text: "In person"
                enabled: full.app.canStartMeeting
                onClicked: full.app.startMeeting(true)
                Accessible.description: inPersonTip.text

                PlasmaComponents3.ToolTip {
                    id: inPersonTip
                    visible: inPersonButton.hovered
                    delay: Kirigami.Units.toolTipDelay
                    text: "A meeting in the room: only the microphone is recorded, and the voices "
                          + "are told apart when you stop (also Meta+Alt+Shift+M)."
                }
            }

            PlasmaComponents3.Label {
                visible: full.app.meetingActive || full.app.saving
                text: full.app.elapsedText
                textFormat: Text.PlainText
                font.pointSize: Kirigami.Theme.defaultFont.pointSize * 1.4
                font.features: { "tnum": 1 }
                color: full.app.meetingRecording && !full.app.saving ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
                opacity: full.app.meetingPaused ? 0.6 : 1
            }

            PlasmaComponents3.Button {
                visible: full.app.meetingActive && !full.app.saving
                icon.name: full.app.meetingPaused ? "media-playback-start" : "media-playback-pause"
                text: full.app.meetingPaused ? "Resume" : "Pause"
                onClicked: full.app.meetingPaused ? full.app.resumeMeeting() : full.app.pauseMeeting()
            }

            PlasmaComponents3.Button {
                visible: full.app.meetingActive && !full.app.saving
                icon.name: "media-playback-stop"
                text: "Stop"
                onClicked: full.app.stopMeeting()
            }

            PlasmaComponents3.BusyIndicator {
                visible: full.app.meetingFinishing || full.app.starting
                Layout.preferredWidth: Kirigami.Units.iconSizes.smallMedium
                Layout.preferredHeight: Kirigami.Units.iconSizes.smallMedium
            }

            PlasmaComponents3.Label {
                visible: full.app.meetingFinishing
                Layout.fillWidth: true
                text: "Saving transcript…"
                elide: Text.ElideRight
            }

            Item {
                visible: !full.app.meetingFinishing
                Layout.fillWidth: true
            }
        }

        PlasmaExtras.DescriptiveLabel {
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            font: Kirigami.Theme.smallFont
            text: full.app.meetingFinishing
                  ? "Transcribing the last chunk; a notification appears when the Markdown file is saved."
                  : full.app.meetingActive
                    ? (full.app.meetingTitle !== "" ? full.app.meetingTitle + " · " : "") + "Meta+Alt+M stops."
                    : "Records microphone + system audio. Meta+Alt+M starts and stops. Middle-click the panel icon does the same."
        }

        // ---- Recent ---------------------------------------------------------
        PlasmaExtras.Heading {
            Layout.fillWidth: true
            Layout.topMargin: Kirigami.Units.smallSpacing
            level: 4
            text: "Recent"
        }

        Repeater {
            model: full.app.recentItems

            delegate: PlasmaComponents3.ItemDelegate {
                id: entry

                required property var modelData

                readonly property bool isMeeting: modelData.kind === "meeting"
                readonly property string statusText: {
                    const s = String(modelData.status || "");
                    if (["", "done", "completed", "complete", "ok", "success"].includes(s))
                        return "";
                    return s === "active" ? "in progress" : s;
                }

                Layout.fillWidth: true
                text: modelData.title || (isMeeting ? "Meeting" : "File")
                onClicked: full.app.openRecent(modelData)
                Accessible.description: subtitle.text

                contentItem: RowLayout {
                    spacing: Kirigami.Units.smallSpacing

                    Kirigami.Icon {
                        Layout.preferredWidth: Kirigami.Units.iconSizes.medium
                        Layout.preferredHeight: Kirigami.Units.iconSizes.medium
                        source: entry.isMeeting ? "meeting-organizer" : "audio-x-generic"
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 0

                        PlasmaComponents3.Label {
                            Layout.fillWidth: true
                            text: entry.text
                            textFormat: Text.PlainText
                            elide: Text.ElideRight
                        }

                        PlasmaComponents3.Label {
                            id: subtitle
                            Layout.fillWidth: true
                            text: [full.app.relativeTime(entry.modelData.time),
                                   full.app.formatLength(entry.modelData.duration),
                                   entry.statusText].filter(s => s !== "").join(" · ")
                            textFormat: Text.PlainText
                            font: Kirigami.Theme.smallFont
                            opacity: 0.7
                            elide: Text.ElideRight
                        }
                    }
                }

                PlasmaComponents3.ToolTip {
                    text: entry.modelData.path || ""
                    visible: entry.hovered && text !== ""
                    delay: Kirigami.Units.toolTipDelay
                }
            }
        }

        PlasmaExtras.DescriptiveLabel {
            Layout.fillWidth: true
            visible: full.app.recentItems.length === 0 || full.app.recentError !== ""
            wrapMode: Text.Wrap
            text: full.app.recentError !== "" ? "Could not load recent items: " + full.app.recentError
                : !full.app.recentLoaded ? "Loading…"
                : "Nothing yet. Meetings and transcribed files will show up here."
            color: full.app.recentError !== "" ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.disabledTextColor
        }

        Item {
            Layout.fillHeight: true
        }
    }

    footer: PlasmaExtras.PlasmoidHeading {
        position: T.ToolBar.Footer

        contentItem: RowLayout {
            spacing: Kirigami.Units.smallSpacing

            PlasmaComponents3.ToolButton {
                icon.name: "document-open"
                text: "Transcribe a file…"
                onClicked: full.app.openApp("transcribe")
            }

            Item {
                Layout.fillWidth: true
            }

            PlasmaComponents3.ToolButton {
                icon.name: "window-new"
                text: "Open BMeet"
                onClicked: full.app.openApp("")
            }
        }
    }
}
