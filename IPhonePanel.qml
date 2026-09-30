import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Hyprland
import qs.Commons
import qs.Ui

// Bar icon + panel. All state comes from `iphone-connect watch` (one JSON line
// per D-Bus change, no polling); every action calls the same CLI. Controls are
// only shown when the backend reports the matching capability.
Panel {
  id: root
  moduleName: "io.github.badr-emil.iphone-connect"
  ipcTarget: "io.github.badr-emil.iphone-connect"

  property var status: null
  property string dialNumber: ""
  property bool keypadOpen: false
  property string lastError: ""
  property real activeSince: 0
  property int elapsed: 0
  property string lastCallState: "idle"

  readonly property string pluginPath: decodeURIComponent(
    Qt.resolvedUrl(".").toString().replace(/^file:\/\//, "")
  )
  readonly property string cli: pluginPath + "backend/iphone-connect"

  readonly property var phone: status ? status.phone : null
  readonly property var call: status ? status.call : null
  readonly property var caps: status && status.capabilities ? status.capabilities : ({})
  readonly property var audioInfo: status ? status.audio : null
  readonly property bool connected: !!(status && status.hfp)
  readonly property string callState: call ? String(call.state) : "idle"
  readonly property bool ringing: callState === "incoming" || callState === "waiting"
  readonly property bool inCall: call !== null && callState !== "disconnected"
  readonly property bool muted: !!(audioInfo && audioInfo.muted)

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string heroStatus: {
    if (!status) return "Starting"
    if (!phone) return "No iPhone paired"
    if (!connected) return phone.connected ? "Connecting hands-free" : "Not connected"
    if (ringing) return "Incoming call"
    if (callState === "dialing") return "Dialing"
    if (callState === "alerting") return "Ringing"
    if (callState === "active") return formatElapsed(elapsed)
    if (callState === "held") return "On hold"
    return "Connected"
  }

  function run(args) {
    lastError = ""
    actionProcess.command = [root.cli].concat(args)
    actionProcess.running = true
  }

  function applyStatus(line) {
    var text = String(line).trim()
    if (text === "") return
    try {
      var next = JSON.parse(text)
    } catch (error) {
      return
    }
    var state = next.call ? String(next.call.state) : "idle"
    if (state === "active" && lastCallState !== "active") activeSince = Date.now()
    if (state !== "active" && state !== "held") activeSince = 0
    if ((state === "incoming" || state === "waiting") && lastCallState !== state
        && setting("openOnIncomingCall", true) !== false && onFocusedMonitor()) root.open()
    if (state === "idle" && lastCallState !== "idle") keypadOpen = false
    lastCallState = state
    status = next
  }

  // Every monitor has its own bar (and its own copy of this widget); only the
  // one on the focused monitor pops open for an incoming call.
  function onFocusedMonitor() {
    var window = button.QsWindow.window
    var focused = Hyprland.focusedMonitor
    if (!window || !window.screen || !focused) return true
    return window.screen.name === focused.name
  }

  function formatElapsed(seconds) {
    var h = Math.floor(seconds / 3600)
    var m = Math.floor((seconds % 3600) / 60)
    var s = seconds % 60
    function pad(v) { return v < 10 ? "0" + v : String(v) }
    return (h > 0 ? pad(h) + ":" : "") + pad(m) + ":" + pad(s)
  }

  function validNumber(value) {
    var cleaned = String(value || "").replace(/[\s\-.\/()]/g, "")
    return /^\+?[0-9*#]{2,21}$/.test(cleaned)
  }

  function pressKey(key) {
    if (inCall && caps.canSendTones) run(["tones", key])
    else dialNumber = dialNumber + key
  }

  function dial() {
    if (!caps.canDial || !validNumber(dialNumber)) return
    run(["call", dialNumber])
  }

  function callerText() {
    if (!call) return ""
    if (call.name) return call.name
    if (call.number) return call.number
    return "Unknown caller"
  }

  visible: !setting("hideWhenNoPhone", false) || connected
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // Long-running event stream: one JSON status line per change.
  Process {
    id: watchProcess
    command: [root.cli, "watch"]
    running: true
    stdout: SplitParser {
      onRead: function(line) { root.applyStatus(line) }
    }
    onExited: restartTimer.start()
  }

  Timer {
    id: restartTimer
    interval: 3000
    onTriggered: watchProcess.running = true
  }

  Process {
    id: actionProcess
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var message = String(text).trim()
        if (message !== "") root.lastError = message.replace(/^iphone-connect:\s*/, "")
      }
    }
  }

  Timer {
    interval: 1000
    repeat: true
    running: root.activeSince > 0
    triggeredOnStart: true
    onTriggered: root.elapsed = Math.max(0, Math.floor((Date.now() - root.activeSince) / 1000))
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: ""
    slotSize: Style.bar.statusSlot
    fontSize: Style.font.caption
    iconComponent: Component {
      Item {
        Text {
          anchors.centerIn: parent
          text: root.inCall ? "󰏶" : "󰄜"
          color: root.ringing ? root.urgent : button.foreground
          opacity: root.connected ? 1 : 0.45
          font.family: button.fontFamily
          font.pixelSize: Style.font.caption
          horizontalAlignment: Text.AlignHCenter
          verticalAlignment: Text.AlignVCenter

          SequentialAnimation on opacity {
            running: root.ringing
            loops: Animation.Infinite
            NumberAnimation { to: 0.3; duration: 450 }
            NumberAnimation { to: 1; duration: 450 }
          }
        }
      }
    }
    tooltipText: root.opened ? "" : "iPhone: " + root.heroStatus
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton && root.caps.canHangup) root.run(["hangup"])
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(320))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: numberField.activeFocus
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: {
        if (root.caps.canAnswer) root.run(["answer"])
        else if (!root.inCall) root.dial()
      }
      onTextKey: function(key) {
        if (/^[0-9*#+]$/.test(key)) root.pressKey(key)
      }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        // ---------- Hero ----------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

          Text {
            id: heroIcon
            text: "󰄜"
            color: root.ringing ? root.urgent : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.display
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(14)
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              text: root.phone ? root.phone.name : "iPhone"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
              elide: Text.ElideRight
              width: parent.width
            }

            Text {
              textFormat: Text.PlainText
              text: root.heroStatus.toUpperCase()
                + (root.status && root.status.transport && root.status.transport.codec && root.inCall
                   ? " · " + root.status.transport.codec : "")
              color: root.ringing ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              font.letterSpacing: 1.2
              elide: Text.ElideRight
              width: parent.width
            }
          }
        }

        // ---------- Not paired / not connected ----------
        Button {
          visible: root.status !== null && !root.phone
          width: parent.width
          iconText: "󰂱"
          text: "Pair iPhone"
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          onClicked: {
            Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", root.cli + " pair"])
            root.close()
          }
        }

        Button {
          visible: root.phone !== null && !root.connected
          width: parent.width
          iconText: "󰂱"
          text: "Connect"
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          onClicked: root.run(["connect"])
        }

        // ---------- Call in progress / incoming ----------
        Column {
          visible: root.inCall
          width: parent.width
          spacing: Style.space(12)

          Text {
            textFormat: Text.PlainText
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            text: root.callerText()
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
            elide: Text.ElideRight
          }

          Text {
            visible: !!(root.call && root.call.name && root.call.number)
            textFormat: Text.PlainText
            width: parent.width
            horizontalAlignment: Text.AlignHCenter
            text: root.call ? root.call.number : ""
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          // Incoming: Decline · Answer
          Row {
            visible: root.ringing
            width: parent.width
            spacing: Style.space(8)

            Button {
              visible: root.caps.canReject === true
              width: (parent.width - parent.spacing) / 2
              iconText: "󰏷"
              text: "Decline"
              foreground: root.urgent
              fontFamily: root.fontFamily
              bordered: true
              onClicked: root.run(["reject"])
            }

            Button {
              visible: root.caps.canAnswer === true
              width: (parent.width - parent.spacing) / 2
              iconText: "󰏲"
              text: "Answer"
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              active: true
              onClicked: root.run(["answer"])
            }
          }

          // Active: Mute · Keypad
          Row {
            visible: !root.ringing
            width: parent.width
            spacing: Style.space(8)

            Button {
              visible: root.caps.canMute === true
              width: (parent.width - parent.spacing) / 2
              iconText: root.muted ? "󰍭" : "󰍬"
              text: root.muted ? "Unmute" : "Mute"
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              active: root.muted
              onClicked: root.run(["mute", root.muted ? "off" : "on"])
            }

            Button {
              visible: root.caps.canSendTones === true
              width: (parent.width - parent.spacing) / 2
              iconText: "󰌌"
              text: "Keypad"
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              active: root.keypadOpen
              onClicked: root.keypadOpen = !root.keypadOpen
            }
          }

          Button {
            visible: !root.ringing && root.caps.canHangup === true
            width: parent.width
            iconText: "󰏷"
            text: "Hang up"
            foreground: root.urgent
            fontFamily: root.fontFamily
            bordered: true
            onClicked: root.run(["hangup"])
          }
        }

        // ---------- Dialer ----------
        Column {
          visible: root.connected && (!root.inCall || (root.keypadOpen && root.caps.canSendTones === true))
          width: parent.width
          spacing: Style.space(10)

          PanelSeparator { foreground: root.foreground; visible: root.inCall }

          TextField {
            id: numberField
            visible: !root.inCall
            width: parent.width
            foreground: root.foreground
            placeholderText: "Phone number"
            text: root.dialNumber
            horizontalAlignment: TextInput.AlignHCenter
            font.pixelSize: Style.font.title
            onTextChanged: root.dialNumber = text
            onAccepted: root.dial()
            Keys.onEscapePressed: function(event) {
              if (text !== "") text = ""
              else root.close()
              event.accepted = true
            }
          }

          Grid {
            id: keypad
            columns: 3
            width: parent.width
            spacing: Style.space(6)
            readonly property real cellWidth: (width - spacing * 2) / 3

            Repeater {
              model: ["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"]
              Button {
                required property string modelData
                width: keypad.cellWidth
                text: modelData
                fontSize: Style.font.title
                foreground: root.foreground
                fontFamily: root.fontFamily
                bordered: true
                verticalPadding: Style.spacing.controlPaddingY + Style.space(4)
                onClicked: root.pressKey(modelData)
              }
            }
          }

          Row {
            visible: !root.inCall
            width: parent.width
            spacing: Style.space(8)

            Button {
              width: parent.width - backspace.width - parent.spacing
              iconText: "󰏲"
              text: "Call"
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              active: root.caps.canDial === true && root.validNumber(root.dialNumber)
              enabled: root.caps.canDial === true && root.validNumber(root.dialNumber)
              onClicked: root.dial()
            }

            Button {
              id: backspace
              iconText: "󰭜"
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              enabled: root.dialNumber !== ""
              onClicked: root.dialNumber = root.dialNumber.slice(0, -1)
            }
          }
        }

        // ---------- Error ----------
        Text {
          visible: root.lastError !== ""
          width: parent.width
          wrapMode: Text.Wrap
          textFormat: Text.PlainText
          text: root.lastError
          color: root.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        // ---------- Audio devices ----------
        Column {
          visible: root.connected && root.audioInfo !== null
          width: parent.width
          spacing: Style.spacing.labelGap

          PanelSeparator { foreground: root.foreground }

          InfoPair { label: "Microphone"; value: root.audioInfo ? root.shortDevice(root.audioInfo.microphone) : "" }
          InfoPair { label: "Output"; value: root.audioInfo ? root.shortDevice(root.audioInfo.output) : "" }
        }
      }
    }
  }

  function shortDevice(name) {
    var value = String(name || "")
    if (value.indexOf("bluez_") === 0) return "Bluetooth"
    if (value.indexOf("usb") >= 0) return "USB audio"
    if (value.indexOf("hdmi") >= 0) return "HDMI"
    if (value.indexOf("analog") >= 0 || value.indexOf("pci") >= 0) return "Built-in audio"
    return value
  }

  component InfoPair: RowLayout {
    property string label: ""
    property string value: ""
    width: parent.width

    Text {
      text: label
      color: root.foreground
      opacity: 0.6
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
    Item { Layout.fillWidth: true }
    Text {
      text: value
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
    }
  }
}
