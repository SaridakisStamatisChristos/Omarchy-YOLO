import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "dev.aether.yolo"
  ipcTarget: "dev.aether.yolo"
  manageIpc: false

  property var snapshot: ({ "job": null, "tasks": [], "counts": {} })
  property string lastError: ""
  readonly property string yoloBin: Quickshell.env("HOME") + "/.local/bin/yolo"
  readonly property var job: snapshot && snapshot.job ? snapshot.job : null
  readonly property var tasks: snapshot && snapshot.tasks ? snapshot.tasks : []
  readonly property string jobState: job ? String(job.state) : "idle"
  readonly property bool busy: ["queued", "planning", "running", "stopping"].indexOf(jobState) >= 0
  readonly property int completedCount: snapshot.counts && snapshot.counts.completed ? snapshot.counts.completed : 0
  readonly property int runningCount: snapshot.counts && snapshot.counts.running ? snapshot.counts.running : 0
  readonly property int totalCount: tasks.length

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function refresh() {
    if (!statusProcess.running) statusProcess.running = true
  }

  function stopJob() {
    if (!job || stopProcess.running) return
    stopProcess.command = [root.yoloBin, "stop", String(job.id)]
    stopProcess.running = true
  }

  function resumeJob() {
    if (!job || resumeProcess.running) return
    resumeProcess.command = [root.yoloBin, "resume", String(job.id)]
    resumeProcess.running = true
  }

  function stateLabel() {
    if (!job) return "No jobs yet"
    var project = String(job.repo).split("/").pop()
    return project + " · " + jobState
  }

  onOpenedChanged: if (opened) refresh()

  Timer {
    interval: root.opened || root.busy ? 2000 : 10000
    repeat: true
    running: true
    onTriggered: root.refresh()
  }

  Process {
    id: statusProcess
    command: [root.yoloBin, "status", "--json"]
    stdout: SplitParser {
      onRead: function(line) {
        try {
          root.snapshot = JSON.parse(line)
          root.lastError = ""
        } catch (e) {
          root.lastError = "Invalid daemon response"
        }
      }
    }
    stderr: SplitParser {
      onRead: function(line) { root.lastError = line }
    }
    onExited: function(code) {
      if (code !== 0 && root.lastError === "") root.lastError = "yolo status failed"
      if (code === 0) root.lastError = ""
    }
  }

  Process {
    id: stopProcess
    onExited: root.refresh()
  }

  Process {
    id: resumeProcess
    onExited: root.refresh()
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refresh(); return "ok" }
    function state(): string { return root.jobState }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.busy ? "Y*" : "Y"
    active: root.jobState === "failed" || root.jobState === "stopped"
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.refresh()
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
    contentWidth: panel.fittedContentWidth(Style.space(460))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onActivateRequested: root.refresh()
      onTextKey: function(t) {
        if (t === "r" || t === "R") root.refresh()
        if ((t === "s" || t === "S") && root.busy) root.stopJob()
      }

      Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        Column {
          id: content
          width: parent.width
          spacing: Style.space(12)

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: "YOLO AGENT CONTROL"
            color: root.bar ? root.bar.foreground : Color.foreground
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.bold: true
            font.pixelSize: Style.font.title
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: root.stateLabel()
            color: Color.popups.text
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.body
            elide: Text.ElideRight
          }

          Rectangle {
            width: parent.width
            height: Style.space(8)
            radius: height / 2
            color: Util.alpha(Color.popups.text, 0.18)

            Rectangle {
              height: parent.height
              width: parent.width * (root.totalCount > 0 ? root.completedCount / root.totalCount : 0)
              radius: height / 2
              color: Color.accent
            }
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: root.totalCount > 0
              ? (root.completedCount + "/" + root.totalCount + " complete · " + root.runningCount + " running")
              : "No task graph loaded"
            color: Util.alpha(Color.popups.text, 0.75)
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.caption
          }

          Repeater {
            model: root.tasks
            delegate: Rectangle {
              required property var modelData
              width: content.width
              height: row.implicitHeight + Style.space(16)
              radius: Style.cornerRadius
              color: Util.alpha(Color.popups.text, 0.07)

              Row {
                id: row
                anchors.fill: parent
                anchors.margins: Style.space(8)
                spacing: Style.space(10)

                Text {
                  textFormat: Text.PlainText
                  width: Style.space(78)
                  text: String(modelData.logical_id) + "  " + String(modelData.state)
                  color: modelData.state === "failed" ? Color.urgent : Color.popups.text
                  font.family: root.bar ? root.bar.fontFamily : Style.font.family
                  font.pixelSize: Style.font.caption
                  font.bold: modelData.state === "running"
                }

                Text {
                  textFormat: Text.PlainText
                  width: row.width - Style.space(92)
                  text: String(modelData.title)
                  color: Color.popups.text
                  font.family: root.bar ? root.bar.fontFamily : Style.font.family
                  font.pixelSize: Style.font.body
                  wrapMode: Text.Wrap
                }
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: root.lastError !== ""
            width: parent.width
            text: root.lastError
            color: Color.urgent
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
          }

          Row {
            spacing: Style.space(8)

            Button {
              text: "Refresh"
              onClicked: root.refresh()
            }
            Button {
              visible: root.busy
              text: "Stop"
              onClicked: root.stopJob()
            }
            Button {
              visible: root.jobState === "failed" || root.jobState === "stopped"
              text: "Resume"
              onClicked: root.resumeJob()
            }
          }
        }
      }
    }
  }
}
