from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

class Bridge(QObject):
    # Signals (Python -> JS)
    coreStateChanged = pyqtSignal(str)
    audioLevelChanged = pyqtSignal(float)
    activeSceneChanged = pyqtSignal(str)
    approvalRequested = pyqtSignal(str)  # JSON-encoded approval request
    
    # Internal Python signal to notify UIController
    actionRequested = pyqtSignal(str)
    approvalDecision = pyqtSignal(str)  # JSON-encoded approval response from JS
    controlResponse = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.transport = None

    def configure_controls(self, runtime, ipc):
        from .control_transport import DesktopControlTransport
        self.transport = DesktopControlTransport(runtime, ipc, self.controlResponse.emit)

    @pyqtSlot(str)
    def controlRequest(self, raw):
        if self.transport:
            self.transport.submit(raw)

    # Slots (JS -> Python)
    @pyqtSlot(str)
    def userInteraction(self, action: str):
        if action == "click":
            self.actionRequested.emit("push_to_talk")
            
    @pyqtSlot()
    def uiReady(self):
        pass

    @pyqtSlot(str, bool)
    def respondToApproval(self, approval_id: str, approved: bool):
        """Called from JS when user approves/denies a pending action."""
        import json
        self.approvalDecision.emit(json.dumps({
            "id": approval_id,
            "approved": approved
        }))
