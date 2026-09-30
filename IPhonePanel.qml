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
  manageIpc: false

  property var status: null
  property string dialNumber: ""
  property bool keypadOpen: false
  property string dialView: "keypad"      // "keypad" | "contacts"
  property var contactEntries: []         // flattened [{name, number}]
  property string contactQuery: ""
  readonly property var filteredContacts: filterContacts(contactQuery)
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
  readonly property bool noiseEnabled: !!(audioInfo && audioInfo.noiseSuppressionEnabled)
  readonly property bool noiseActive: !!(audioInfo && audioInfo.noiseSuppression)

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
    if (callState === "active") return formatElapsed(elapsed) + (caps.canTakeOver ? " · on iPhone" : "")
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

  function loadContacts(output) {
    var list = []
    try {
      var data = JSON.parse(String(output).trim() || "[]")
      for (var i = 0; i < data.length; i++)
        for (var j = 0; j < data[i].numbers.length; j++)
          list.push({ name: String(data[i].name), number: String(data[i].numbers[j]) })
    } catch (error) {
      list = []
    }
    contactEntries = list
  }

  // Match every word of the query against the name, or digits against the number.
  function filterContacts(query) {
    var q = String(query || "").toLowerCase().trim()
    var digits = q.replace(/[^0-9]/g, "")
    var words = q.split(/\s+/).filter(function(w) { return w !== "" })
    var result = []
    for (var i = 0; i < contactEntries.length && result.length < 60; i++) {
      var entry = contactEntries[i]
      var name = entry.name.toLowerCase()
      var ok = words.every(function(w) { return name.indexOf(w) >= 0 })
      if (!ok && digits.length >= 3) ok = entry.number.replace(/[^0-9]/g, "").indexOf(digits) >= 0
      if (ok) result.push(entry)
    }
    return result
  }

  function callContact(entry) {
    if (!caps.canDial || !validNumber(entry.number)) return
    dialNumber = entry.number
    run(["call", entry.number])
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

  // Stop the event stream when this widget instance goes away (plugin reload).
  Component.onDestruction: {
    restartTimer.stop()
    watchProcess.running = false
  }

  visible: !setting("hideWhenNoPhone", false) || connected
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: {
    if (opened && !contactsProcess.running) contactsProcess.running = true
    if (!opened) contactQuery = ""
  }

  Process {
    id: contactsProcess
    command: [root.cli, "contacts", "list", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.loadContacts(text)
    }
  }

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

  IpcHandler {
    target: "io.github.badr-emil.iphone-connect"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function toggleMute(): string { root.run(["mute", root.muted ? "off" : "on"]); return root.muted ? "was muted" : "was unmuted" }
    function contacts(): void { root.dialView = "contacts"; root.open() }
    function keypad(): void { root.dialView = "keypad"; root.open() }
    function state(): string { return JSON.stringify({ muted: root.muted, call: root.callState }) }
  }

  // Mute and similar changes emit no D-Bus signal, so refresh once after every action.
  Process {
    id: statusOnceProcess
    command: [root.cli, "status", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyStatus(text)
    }
  }

  Process {
    id: actionProcess
    onExited: if (!statusOnceProcess.running) statusOnceProcess.running = true
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
          text: root.inCall && root.muted ? "󰍭" : (root.inCall ? "󰏶" : "󰄜")
          color: root.ringing || (root.inCall && root.muted) ? root.urgent : button.foreground
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
      blocked: numberField.activeFocus || contactSearch.activeFocus
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: {
        if (root.caps.canAnswer) root.run(["answer"])
        else if (!root.inCall) root.dial()
      }
      onTextKey: function(key) {
        if (root.dialView === "contacts" && !root.inCall) {
          contactSearch.forceActiveFocus()
          contactSearch.text = contactSearch.text + key
        } else if (/^[0-9*#+]$/.test(key)) root.pressKey(key)
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
                + (root.inCall && root.muted ? " · MUTED" : "")
                + (root.status && root.status.transport && root.status.transport.codec && root.inCall
                   ? " · " + root.status.transport.codec : "")
              color: root.ringing || (root.inCall && root.muted) ? root.urgent : root.dim
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
              text: root.muted ? "Muted" : "Mute"
              tooltipText: root.muted ? "Microphone is off. Click to turn it back on." : "Turn the microphone off"
              foreground: root.muted ? root.urgent : root.foreground
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

          // Call running on the iPhone (dialed or answered there): offer to move it here.
          Button {
            visible: root.caps.canTakeOver === true
            width: parent.width
            iconText: "󰍹"
            text: "Take call on this PC"
            tooltipText: "The call is on the iPhone. Move its audio to this PC's microphone and speakers."
            foreground: root.foreground
            fontFamily: root.fontFamily
            bordered: true
            active: true
            onClicked: root.run(["take"])
          }

          // Unmistakable mute state: the caller cannot hear you while this shows.
          Rectangle {
            visible: root.inCall && !root.ringing && root.muted
            width: parent.width
            implicitHeight: muteBanner.implicitHeight + Style.space(12)
            radius: Style.cornerRadius
            color: Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.18)
            border.color: root.urgent
            border.width: 1

            Text {
              id: muteBanner
              anchors.centerIn: parent
              width: parent.width - Style.space(16)
              horizontalAlignment: Text.AlignHCenter
              wrapMode: Text.Wrap
              text: "󰍭  Microphone off – the caller can't hear you"
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              font.bold: true
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

          // Keypad | Contacts
          Row {
            visible: !root.inCall && root.contactEntries.length > 0
            width: parent.width
            spacing: Style.space(6)

            Button {
              width: (parent.width - parent.spacing) / 2
              iconText: "󰌌"
              text: "Keypad"
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              active: root.dialView === "keypad"
              onClicked: root.dialView = "keypad"
            }

            Button {
              width: (parent.width - parent.spacing) / 2
              iconText: "󰛋"
              text: "Contacts"
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              active: root.dialView === "contacts"
              onClicked: {
                root.dialView = "contacts"
                contactSearch.forceActiveFocus()
              }
            }
          }

          // ---------- Contacts ----------
          Column {
            visible: !root.inCall && root.dialView === "contacts"
            width: parent.width
            spacing: Style.space(6)

            TextField {
              id: contactSearch
              width: parent.width
              foreground: root.foreground
              placeholderText: "Search " + root.contactEntries.length + " numbers"
              text: root.contactQuery
              onTextChanged: root.contactQuery = text
              onAccepted: if (root.filteredContacts.length > 0) root.callContact(root.filteredContacts[0])
              Keys.onEscapePressed: function(event) {
                if (text !== "") text = ""
                else root.close()
                event.accepted = true
              }
            }

            Flickable {
              width: parent.width
              height: Math.min(contactColumn.implicitHeight, Style.space(320))
              contentHeight: contactColumn.implicitHeight
              clip: true
              boundsBehavior: Flickable.StopAtBounds

              Column {
                id: contactColumn
                width: parent.width
                spacing: Style.space(2)

                Repeater {
                  model: root.filteredContacts
                  ContactRow {
                    required property var modelData
                    width: contactColumn.width
                    entry: modelData
                  }
                }
              }
            }

            Text {
              visible: root.filteredContacts.length === 0
              width: parent.width
              horizontalAlignment: Text.AlignHCenter
              text: "No contact found"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
          }

          TextField {
            id: numberField
            visible: !root.inCall && root.dialView === "keypad"
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
            visible: root.inCall || root.dialView === "keypad"
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
            visible: !root.inCall && root.dialView === "keypad"
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

          // WebRTC noise suppression + echo cancellation (fan noise, hum, echo)
          Button {
            width: parent.width
            iconText: "󰕾"
            text: "Noise suppression: " + (root.noiseEnabled ? (root.inCall ? (root.noiseActive ? "active" : "starting") : "on") : "off")
            tooltipText: "Removes fan noise and echo from your microphone during calls (WebRTC)"
            foreground: root.foreground
            fontFamily: root.fontFamily
            fontSize: Style.font.bodySmall
            bordered: true
            active: root.noiseEnabled
            onClicked: root.run(["noise", root.noiseEnabled ? "off" : "on"])
          }
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

  component ContactRow: CursorSurface {
    id: contactRow
    property var entry: ({ name: "", number: "" })
    foreground: root.foreground
    hasCursor: rowMouse.containsMouse
    implicitHeight: contactContent.implicitHeight + Style.spacing.rowPaddingX

    MouseArea {
      id: rowMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.callContact(contactRow.entry)
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(8)
      spacing: Style.space(8)

      ColumnLayout {
        id: contactContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: contactRow.entry.name
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          Layout.fillWidth: true
          textFormat: Text.PlainText
          text: contactRow.entry.number
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      Text {
        text: "󰏲"
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        Layout.alignment: Qt.AlignVCenter
      }
    }
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
