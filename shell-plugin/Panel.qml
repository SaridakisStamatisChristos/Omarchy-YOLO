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

  property var snapshot: ({
    "job": null,
    "tasks": [],
    "counts": {},
    "runtime": {},
    "telemetry": {},
    "last_events": []
  })
  property string lastError: ""
  readonly property string yoloBin: Quickshell.env("HOME") + "/.local/bin/yolo"
  readonly property var job: snapshot && snapshot.job ? snapshot.job : null
  readonly property var tasks: snapshot && snapshot.tasks ? snapshot.tasks : []
  readonly property var runtime: snapshot && snapshot.runtime ? snapshot.runtime : ({})
  readonly property var telemetry: snapshot && snapshot.telemetry ? snapshot.telemetry : ({})
  readonly property var lastEvents: snapshot && snapshot.last_events ? snapshot.last_events : []
  readonly property string jobState: job ? String(job.state) : "idle"
  readonly property bool busy: ["queued", "planning", "running", "stopping"].indexOf(jobState) >= 0
  readonly property int completedCount: snapshot.counts && snapshot.counts.completed ? snapshot.counts.completed : 0
  readonly property int activeCount:
    (snapshot.counts && snapshot.counts.running ? snapshot.counts.running : 0)
    + (snapshot.counts && snapshot.counts.reviewing ? snapshot.counts.reviewing : 0)
    + (snapshot.counts && snapshot.counts.integrating ? snapshot.counts.integrating : 0)
  readonly property int problemCount:
    (snapshot.counts && snapshot.counts.failed ? snapshot.counts.failed : 0)
    + (snapshot.counts && snapshot.counts.blocked ? snapshot.counts.blocked : 0)
    + (snapshot.counts && snapshot.counts.stopped ? snapshot.counts.stopped : 0)
  readonly property int totalCount: tasks.length
  readonly property int workersActive: runtime.workers_active ? Number(runtime.workers_active) : 0
  readonly property int workerCapacity: runtime.worker_capacity ? Number(runtime.worker_capacity) : 0
  readonly property int workersWaiting: runtime.workers_waiting ? Number(runtime.workers_waiting) : 0
  readonly property int attemptsTotal: telemetry.attempts_total ? Number(telemetry.attempts_total) : 0

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

  function durationLabel(rawSeconds) {
    var seconds = Math.max(0, Math.floor(Number(rawSeconds) || 0))
    if (seconds < 60) return seconds + "s"
    if (seconds < 3600) return Math.floor(seconds / 60) + "m " + (seconds % 60) + "s"
    return Math.floor(seconds / 3600) + "h " + Math.floor((seconds % 3600) / 60) + "m"
  }

  function taskTelemetry(task) {
    var attempt = task && task.latest_attempt ? task.latest_attempt : null
    var age = root.durationLabel(task && task.state_age_seconds ? task.state_age_seconds : 0)
    if (!attempt) return "state age " + age
    return String(attempt.agent) + " · attempt " + String(attempt.number)
      + " · " + root.durationLabel(attempt.elapsed_seconds) + " · state age " + age
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
    text: root.problemCount > 0 || root.jobState === "failed" ? "Y!" : (root.activeCount > 0 ? "Y" + root.activeCount : "Y")
    active: root.jobState === "failed" || root.jobState === "stopped" || root.problemCount > 0
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
            text: root.job
              ? ("runtime " + root.durationLabel(root.telemetry.job_elapsed_seconds)
                 + " · state age " + root.durationLabel(root.telemetry.state_age_seconds))
              : "Persistent local daemon"
            color: Util.alpha(Color.popups.text, 0.68)
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
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
              ? (root.completedCount + "/" + root.totalCount + " complete · " + root.activeCount
                 + " active" + (root.problemCount > 0 ? " · " + root.problemCount + " blocked/failed" : ""))
              : "No task graph loaded"
            color: Util.alpha(Color.popups.text, 0.75)
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.caption
          }

          Rectangle {
            width: parent.width
            height: capacityColumn.implicitHeight + Style.space(16)
            radius: Style.cornerRadius
            color: Util.alpha(Color.popups.text, 0.07)

            Column {
              id: capacityColumn
              anchors.fill: parent
              anchors.margins: Style.space(8)
              spacing: Style.space(4)

              Text {
                textFormat: Text.PlainText
                width: parent.width
                text: "Workers " + root.workersActive + "/" + root.workerCapacity
                  + (root.workersWaiting > 0 ? " · " + root.workersWaiting + " waiting" : "")
                  + " · repo locks " + (root.runtime.repositories_active || 0)
                color: Color.popups.text
                font.family: root.bar ? root.bar.fontFamily : Style.font.family
                font.pixelSize: Style.font.caption
              }

              Text {
                textFormat: Text.PlainText
                width: parent.width
                text: "Attempts " + root.attemptsTotal
                  + " · passed " + (root.telemetry.states && root.telemetry.states.passed ? root.telemetry.states.passed : 0)
                  + " · failed " + (root.telemetry.states && root.telemetry.states.failed ? root.telemetry.states.failed : 0)
                  + " · busy " + root.durationLabel(root.runtime.worker_busy_seconds_total)
                color: Util.alpha(Color.popups.text, 0.72)
                font.family: root.bar ? root.bar.fontFamily : Style.font.family
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }
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

                Column {
                  width: row.width - Style.space(92)
                  spacing: Style.space(3)

                  Text {
                    textFormat: Text.PlainText
                    width: parent.width
                    text: String(modelData.title)
                    color: Color.popups.text
                    font.family: root.bar ? root.bar.fontFamily : Style.font.family
                    font.pixelSize: Style.font.body
                    wrapMode: Text.Wrap
                  }

                  Text {
                    textFormat: Text.PlainText
                    width: parent.width
                    text: root.taskTelemetry(modelData)
                    color: Util.alpha(Color.popups.text, 0.62)
                    font.family: root.bar ? root.bar.fontFamily : Style.font.family
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }
                }
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: root.lastEvents.length > 0
            width: parent.width
            text: root.lastEvents.length > 0
              ? ("Latest · " + String(root.lastEvents[root.lastEvents.length - 1].kind))
              : ""
            color: Util.alpha(Color.popups.text, 0.65)
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
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
