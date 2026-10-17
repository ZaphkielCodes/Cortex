import os
from PyQt6.QtCore import QUrl
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWidgets import QVBoxLayout, QWidget
from PyQt6.QtWebEngineCore import QWebEngineSettings, QWebEnginePage, QWebEngineProfile
from .bridge import Bridge

class SecureWebPage(QWebEnginePage):
    def acceptNavigationRequest(self, url, _type, isMainFrame):
        # Prevent arbitrary navigation
        if url.scheme() in ["file", "qrc", "data"]:
            return True
        return False

class WorkspaceView(QWidget):
    def __init__(self, bridge: Bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        
        self.view = QWebEngineView(self)
        self.page = SecureWebPage(self.view)
        self.view.setPage(self.page)
        self.layout.addWidget(self.view)
        
        # Harden QWebEngine Profile
        profile = self.page.profile()
        settings = profile.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PdfViewerEnabled, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        
        # Setup WebChannel
        self.channel = QWebChannel(self.page)
        self.channel.registerObject("bridge", self.bridge)
        self.page.setWebChannel(self.channel)
        
        # Load the bundled React app
        dist_path = os.path.join(os.path.dirname(__file__), "dist", "index.html")
        if not os.path.exists(dist_path):
            dist_path = os.path.join(os.path.dirname(__file__), "cortex_core", "dist", "index.html")
        self.view.setUrl(QUrl.fromLocalFile(dist_path))
