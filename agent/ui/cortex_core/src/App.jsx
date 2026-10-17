import { useState, useEffect, useRef, useMemo } from 'react'
import { CortexOrb } from './CortexOrb'

const API_BASE = 'http://127.0.0.1:8000'
const WS_BASE = 'ws://127.0.0.1:8000'

export default function App() {
  // Navigation / Drawer state (null means assistant only, string opens secondary drawer)
  const [activeDrawer, setActiveDrawer] = useState(null)
  
  // Real Backend State
  const [identity, setIdentity] = useState({
    product_name: 'Cortex',
    version: '0.1.0',
    build_identifier: 'cortex-hybrid-2026.10',
    current_node: 'asus',
    role: 'primary_admin',
    architecture_mode: 'hybrid'
  })
  const [devices, setDevices] = useState([])
  const [tasks, setTasks] = useState([])
  const [approvals, setApprovals] = useState([])
  const [transfers, setTransfers] = useState([])
  const [worldState, setWorldState] = useState({ summary: {}, foreground_history: [] })
  const [systemHealth, setSystemHealth] = useState({ models: [], providers: [], integrations: [], capabilities: [] })
  const [autonomyEnabled, setAutonomyEnabled] = useState(true)
  const [activities, setActivities] = useState([])
  
  // Conversation timeline
  const [messages, setMessages] = useState([
    {
      id: 'init-1',
      sender: 'cortex',
      text: 'Cortex OS is online. Neural core active on asus node.',
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      status: 'success'
    }
  ])
  const [inputText, setInputText] = useState('')
  const [isSending, setIsSending] = useState(false)
  const [isVoiceActive, setIsVoiceActive] = useState(false)
  
  // Realtime & Core State
  const [wsConnected, setWsConnected] = useState(true)
  const [isReconnecting, setIsReconnecting] = useState(false)
  const [coreState, setCoreState] = useState('idle')
  const [audioLevel, setAudioLevel] = useState(0)
  const [toastMessage, setToastMessage] = useState('')

  const wsRef = useRef(null)
  const reconnectTimeoutRef = useRef(null)
  const messagesEndRef = useRef(null)

  // Toast auto-dismiss
  useEffect(() => {
    if (!toastMessage) return
    const t = setTimeout(() => setToastMessage(''), 3500)
    return () => clearTimeout(t)
  }, [toastMessage])

  // Scroll messages to bottom on update
  useEffect(() => {
    if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages])

  // Fetch Authoritative Snapshot (REST fallback & reconciliation)
  const fetchCanonicalState = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/control/state`)
      if (res.ok) {
        const data = await res.json()
        applyStateSnapshot(data)
      }
    } catch (e) {
      console.warn('Failed to fetch canonical state via REST:', e)
    }
  }

  // Apply state snapshot
  const applyStateSnapshot = (data) => {
    if (!data) return
    if (data.identity) setIdentity(data.identity)
    if (data.devices) setDevices(data.devices)
    if (data.tasks) setTasks(data.tasks)
    if (data.approvals) setApprovals(data.approvals)
    if (data.transfers) setTransfers(data.transfers)
    if (data.world_state) setWorldState(data.world_state)
    if (data.system_health) setSystemHealth(data.system_health)
    if (data.safety && typeof data.safety.autonomy_enabled === 'boolean') {
      setAutonomyEnabled(data.safety.autonomy_enabled)
    }
    if (Array.isArray(data.activities) && data.activities.length > 0) {
      setActivities(data.activities)
    }
  }

  // WebSocket Connection Management (Live events + Reconnect reconciliation)
  useEffect(() => {
    let unmounted = false

    const connectWebSocket = () => {
      if (unmounted) return

      try {
        const ws = new WebSocket(`${WS_BASE}/ws-control`)
        wsRef.current = ws

        ws.onopen = () => {
          setWsConnected(true)
          setIsReconnecting(false)
          ws.send(JSON.stringify({ type: 'get_state' }))
        }

        ws.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data)
            const etype = msg.event
            const payload = msg.data

            if (etype === 'initial_state') {
              applyStateSnapshot(payload)
            } else if (etype === 'activity') {
              setActivities((prev) => [payload, ...prev.slice(0, 49)])
            } else if (etype === 'device_state') {
              setDevices(payload)
            } else if (etype === 'task_state') {
              setTasks(payload)
            } else if (etype === 'approval_state') {
              setApprovals(payload)
            } else if (etype === 'transfer_state') {
              setTransfers(payload)
            } else if (etype === 'world_state') {
              setWorldState((prev) => ({ ...prev, summary: payload }))
            } else if (etype === 'safety_state') {
              if (typeof payload.autonomy_enabled === 'boolean') {
                setAutonomyEnabled(payload.autonomy_enabled)
              }
            } else if (etype === 'conversation') {
              setMessages((prev) => [
                ...prev,
                {
                  id: `resp-${Date.now()}`,
                  sender: 'cortex',
                  text: payload.response_text,
                  status: payload.status,
                  actionPlan: payload.action_plan,
                  isVoice: payload.is_voice,
                  timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                }
              ])
              setCoreState('idle')
            }
          } catch (err) {
            console.error('Error parsing WS message:', err)
          }
        }

        ws.onerror = () => {
          setWsConnected(false)
        }

        ws.onclose = () => {
          setWsConnected(false)
          if (!unmounted) {
            setIsReconnecting(true)
            reconnectTimeoutRef.current = setTimeout(connectWebSocket, 2000)
          }
        }
      } catch (err) {
        setWsConnected(false)
        setIsReconnecting(true)
        reconnectTimeoutRef.current = setTimeout(connectWebSocket, 2500)
      }
    }

    connectWebSocket()
    fetchCanonicalState()

    return () => {
      unmounted = true
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current)
      if (wsRef.current) wsRef.current.close()
    }
  }, [])

  // Connect to QWebChannel (Desktop Host Bridge) if inside PyQt
  useEffect(() => {
    if (window.QWebChannel && window.qt && window.qt.webChannelTransport) {
      new window.QWebChannel(window.qt.webChannelTransport, (channel) => {
        const pyBridge = channel.objects.bridge
        if (!pyBridge) return

        if (pyBridge.coreStateChanged) {
          pyBridge.coreStateChanged.connect((st) => setCoreState(st))
        }
        if (pyBridge.audioLevelChanged) {
          pyBridge.audioLevelChanged.connect((lvl) => setAudioLevel(lvl))
        }
        if (pyBridge.approvalRequested) {
          pyBridge.approvalRequested.connect(() => {
            fetchCanonicalState()
            setActiveDrawer('approvals')
          })
        }
      })
    }
  }, [])

  // Expose state mutators for testing and automation validation
  useEffect(() => {
    window.__cortexCore = {
      setCoreState,
      setIsVoiceActive,
      setIsSending,
      setApprovals,
      setTasks,
      setMessages,
      setActiveDrawer,
      fetchCanonicalState,
    }
  }, [])

  // Derive authoritative effective orb state from live runtime
  const effectiveOrbState = useMemo(() => {
    if (!wsConnected && !window.qt) return 'offline'
    if (coreState && coreState !== 'idle') return coreState
    if (approvals.length > 0) return 'approval required'
    if (isVoiceActive) return 'listening'
    if (isSending) return 'thinking'
    const runningTasks = tasks.filter(t => t.status === 'RUNNING' || t.status === 'ACTIVE')
    if (runningTasks.length > 0) return 'acting'
    const waitingTasks = tasks.filter(t => t.status === 'WAITING' || t.status === 'PAUSED')
    if (waitingTasks.length > 0) return 'waiting'
    const latestMessage = messages[messages.length - 1]
    if (latestMessage && latestMessage.status === 'error') return 'error'
    return 'idle'
  }, [wsConnected, coreState, approvals.length, isVoiceActive, isSending, tasks, messages])

  // Latest safe operational activity
  const latestActivity = useMemo(() => {
    if (activities && activities.length > 0) {
      return activities[0].label
    }
    if (effectiveOrbState === 'listening') return 'Listening for speech input...'
    if (effectiveOrbState === 'thinking') return 'Semantic planning in progress...'
    if (effectiveOrbState === 'acting') return 'Executing bounded action sequence...'
    if (effectiveOrbState === 'approval required') return 'Action paused: Tier 3 human approval required'
    return 'Cortex OS nominal · Neural core ready'
  }, [activities, effectiveOrbState])

  // Actions
  const sendMessage = async (e, overrideText = null) => {
    if (e) e.preventDefault()
    const text = (overrideText || inputText).trim()
    if (!text || isSending) return

    const userMsg = {
      id: `usr-${Date.now()}`,
      sender: 'user',
      text,
      isVoice: isVoiceActive,
      timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    }
    setMessages((prev) => [...prev, userMsg])
    setInputText('')
    setIsSending(true)
    setCoreState('thinking')

    try {
      const res = await fetch(`${API_BASE}/api/control/message`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text,
          device_id: identity.current_node || 'asus',
          is_voice: isVoiceActive
        })
      })

      if (res.ok) {
        const data = await res.json()
        setMessages((prev) => [
          ...prev,
          {
            id: `resp-${Date.now()}`,
            sender: 'cortex',
            text: data.response_text,
            status: data.status,
            actionPlan: data.action_plan,
            isVoice: isVoiceActive,
            timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
          }
        ])
        setCoreState('speaking')
        setTimeout(() => setCoreState('idle'), 2200)
      } else {
        const errText = await res.text()
        setMessages((prev) => [
          ...prev,
          {
            id: `err-${Date.now()}`,
            sender: 'cortex',
            text: `Action execution error: ${errText || 'Request failed'}`,
            status: 'error',
            timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
          }
        ])
        setCoreState('error')
        setTimeout(() => setCoreState('idle'), 3000)
      }
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          id: `err-${Date.now()}`,
          sender: 'cortex',
          text: `Network connection: ${err.message}`,
          status: 'error',
          timestamp: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
        }
      ])
      setCoreState('error')
      setTimeout(() => setCoreState('idle'), 3000)
    } finally {
      setIsSending(false)
      fetchCanonicalState()
    }
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendMessage()
    }
  }

  const toggleAutonomy = async () => {
    const nextVal = !autonomyEnabled
    try {
      const res = await fetch(`${API_BASE}/api/control/autonomy`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: nextVal })
      })
      if (res.ok) {
        setAutonomyEnabled(nextVal)
        setToastMessage(`Global Autonomy ${nextVal ? 'Activated' : 'Stopped'}`)
      }
    } catch (e) {
      setToastMessage(`Failed to set autonomy: ${e.message}`)
    }
  }

  const stopAllTasks = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/control/tasks/pause_all`, {
        method: 'POST'
      })
      if (res.ok) {
        setToastMessage('Emergency stop: All active tasks paused.')
        fetchCanonicalState()
      }
    } catch (e) {
      setToastMessage(`Failed to stop tasks: ${e.message}`)
    }
  }

  const stopTask = async (taskId) => {
    try {
      const res = await fetch(`${API_BASE}/api/control/tasks/${taskId}/stop`, {
        method: 'POST'
      })
      if (res.ok) {
        setToastMessage(`Task cancelled.`)
        fetchCanonicalState()
      }
    } catch (e) {
      setToastMessage(`Failed to stop task: ${e.message}`)
    }
  }

  const resolveApproval = async (commandId, decision) => {
    try {
      const res = await fetch(`${API_BASE}/api/control/approvals/${commandId}/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision })
      })
      if (res.ok) {
        setToastMessage(`Approval ${decision === 'approve' ? 'GRANTED' : 'DENIED'}`)
        fetchCanonicalState()
      }
    } catch (e) {
      setToastMessage(`Error resolving approval: ${e.message}`)
    }
  }

  const cancelTransfer = async (transferId) => {
    try {
      const res = await fetch(`${API_BASE}/api/control/transfers/${transferId}/cancel`, {
        method: 'POST'
      })
      if (res.ok) {
        setToastMessage(`Transfer ${transferId} cancelled.`)
        fetchCanonicalState()
      }
    } catch (e) {
      setToastMessage(`Error cancelling transfer: ${e.message}`)
    }
  }

  const resumeTransfer = async (transferId) => {
    try {
      const res = await fetch(`${API_BASE}/api/control/transfers/${transferId}/resume`, {
        method: 'POST'
      })
      if (res.ok) {
        setToastMessage(`Transfer ${transferId} resumed.`)
        fetchCanonicalState()
      }
    } catch (e) {
      setToastMessage(`Error resuming transfer: ${e.message}`)
    }
  }

  const disconnectDevice = async (deviceId) => {
    try {
      const res = await fetch(`${API_BASE}/api/control/devices/${deviceId}/disconnect`, {
        method: 'POST'
      })
      if (res.ok) {
        setToastMessage(`Device ${deviceId} disconnected.`)
        fetchCanonicalState()
      }
    } catch (e) {
      setToastMessage(`Failed to disconnect device: ${e.message}`)
    }
  }

  const toggleDrawer = (tab) => {
    setActiveDrawer(prev => prev === tab ? null : tab)
  }

  return (
    <div className="cortex-app">
      {/* Toast Notification */}
      {toastMessage && (
        <div className="cc-toast">
          <span>⚡</span>
          <span>{toastMessage}</span>
        </div>
      )}

      {/* ── Unified Top Navigation / Status Header ──────────────────────── */}
      <header className="cc-topbar">
        {/* Brand & Identity */}
        <div className="cc-topbar-brand">
          <div className="cc-brand-badge">
            <span className="cc-badge-core">CX</span>
            <div className="cc-brand-titles">
              <span className="cc-brand-name">CORTEX</span>
              <span className="cc-brand-sub">OS ASSISTANT</span>
            </div>
          </div>

          <div className="cc-node-pill" title={`${identity.current_node} · ${identity.role}`}>
            <span className={`cc-live-dot ${wsConnected ? 'online' : 'offline'}`} />
            <span className="cc-node-name">{identity.current_node.toUpperCase()}</span>
            <span className="cc-arch-tag">{identity.architecture_mode}</span>
          </div>
        </div>

        {/* Live Safe Operational Activity Ticker (Center) */}
        <div className="cc-activity-ticker" title="Operational activity summary (no chain-of-thought)">
          <span className="cc-ticker-icon">⚡</span>
          <span className="cc-ticker-text">{latestActivity}</span>
        </div>

        {/* System Controls & Drawer Triggers (Right) */}
        <div className="cc-topbar-actions">
          {/* Global Autonomy Kill Switch */}
          <button
            className={`cc-autonomy-btn ${autonomyEnabled ? 'active' : 'stopped'}`}
            onClick={toggleAutonomy}
            title="Toggle autonomous goal execution"
          >
            <span className="cc-switch-dot" />
            <span className="cc-switch-label">{autonomyEnabled ? 'AUTONOMY ON' : 'STOPPED'}</span>
          </button>

          {/* Emergency Stop All Tasks */}
          <button
            className="cc-icon-btn cc-btn-danger"
            onClick={stopAllTasks}
            title="Emergency pause of all running tasks"
          >
            🛑 Stop
          </button>

          <div className="cc-divider-v" />

          {/* Secondary Drawer Toggle Buttons */}
          <button
            className={`cc-drawer-toggle ${activeDrawer === 'tasks' ? 'active' : ''}`}
            onClick={() => toggleDrawer('tasks')}
            title="Active Tasks & Goals"
          >
            📋 Tasks
            {tasks.length > 0 && <span className="cc-pill-badge">{tasks.length}</span>}
          </button>

          <button
            className={`cc-drawer-toggle ${activeDrawer === 'devices' ? 'active' : ''}`}
            onClick={() => toggleDrawer('devices')}
            title="Execution Nodes & Devices"
          >
            🖥️ Nodes
            {devices.length > 0 && <span className="cc-pill-badge">{devices.length}</span>}
          </button>

          <button
            className={`cc-drawer-toggle ${activeDrawer === 'approvals' ? 'active' : ''} ${approvals.length > 0 ? 'alert-pulse' : ''}`}
            onClick={() => toggleDrawer('approvals')}
            title="Action Approvals Queue"
          >
            🛡️ Approvals
            {approvals.length > 0 && <span className="cc-pill-badge danger">{approvals.length}</span>}
          </button>

          <button
            className={`cc-drawer-toggle ${activeDrawer === 'transfers' ? 'active' : ''}`}
            onClick={() => toggleDrawer('transfers')}
            title="File Transfers"
          >
            📦 Transfers
            {transfers.length > 0 && <span className="cc-pill-badge">{transfers.length}</span>}
          </button>

          <button
            className={`cc-drawer-toggle ${activeDrawer === 'system_health' ? 'active' : ''}`}
            onClick={() => toggleDrawer('system_health')}
            title="System & Health Status"
          >
            🔬 System
          </button>

          <button
            className={`cc-drawer-toggle ${activeDrawer === 'world_state' ? 'active' : ''}`}
            onClick={() => toggleDrawer('world_state')}
            title="World State & Sensory Grounding"
          >
            🌐 World
          </button>

          <button
            className={`cc-drawer-toggle ${activeDrawer === 'privacy' ? 'active' : ''}`}
            onClick={() => toggleDrawer('privacy')}
            title="Privacy & Data Routing"
          >
            🔒 Privacy
          </button>
        </div>
      </header>

      {/* ── Main Layout: Hero Assistant Stage + Slide-out Drawer ─────────── */}
      <div className="cc-body-container">
        {/* Central Assistant Stage */}
        <main className={`cc-assistant-stage ${activeDrawer ? 'drawer-opened' : ''}`}>
          {/* Central 3D Cortex Orb Presence */}
          <div className="cc-hero-orb-wrapper">
            <div className="cc-orb-canvas-container">
              <CortexOrb status={effectiveOrbState} audioLevel={audioLevel} />
            </div>

            {/* Dynamic State Pill */}
            <div className={`cc-orb-state-indicator state-${effectiveOrbState.replace(/\s+/g, '_')}`}>
              <span className="cc-state-dot" />
              <span className="cc-state-text">{effectiveOrbState.toUpperCase()}</span>
            </div>
          </div>

          {/* Central Conversation Stream & Action Cards */}
          <div className="cc-conversation-container">
            <div className="cc-messages-feed">
              {messages.map((m) => (
                <div key={m.id} className={`cc-message-row ${m.sender}`}>
                  <div className={`cc-avatar ${m.sender}`}>
                    {m.sender === 'cortex' ? 'CX' : 'YOU'}
                  </div>
                  <div className="cc-bubble-wrap">
                    <div className="cc-bubble">
                      <div className="cc-bubble-text">{m.text}</div>

                      {/* Inline Action Plan Card if present */}
                      {m.actionPlan && (
                        <div className="cc-inline-card action-card">
                          <div className="cc-card-top">
                            <span className="cc-card-label">ACTION PLAN</span>
                            <span className="cc-status-pill success">{m.status || 'EXECUTED'}</span>
                          </div>
                          <div className="cc-card-details">
                            <div>Target: <strong>{m.actionPlan.target_device || 'local'}</strong></div>
                            <div>Goal: {m.actionPlan.goal}</div>
                            {m.actionPlan.actions && (
                              <div className="cc-card-sub">
                                Steps: {m.actionPlan.actions.length} action(s) planned
                              </div>
                            )}
                          </div>
                        </div>
                      )}

                      {/* Meta timestamp & voice badge */}
                      <div className="cc-message-meta">
                        <span>{m.timestamp}</span>
                        {m.isVoice && <span className="cc-voice-badge">🎤 Voice</span>}
                      </div>
                    </div>
                  </div>
                </div>
              ))}

              {/* Inline Approval Cards if pending confirmations exist */}
              {approvals.map((appr) => (
                <div key={appr.command_id} className="cc-inline-approval-card">
                  <div className="cc-appr-header">
                    <span className="cc-appr-badge">⚠️ CONFIRMATION REQUIRED</span>
                    <span className="cc-appr-tier">Tier {appr.tier || 3}</span>
                  </div>
                  <h4 className="cc-appr-action">{appr.action_summary}</h4>
                  <p className="cc-appr-risk">{appr.privacy_risk}</p>
                  <div className="cc-appr-meta">
                    <span>Target: <strong>{appr.target_device}</strong></span>
                    <span>Leaves Local: <strong>{appr.data_leaves_local ? 'YES' : 'NO'}</strong></span>
                  </div>
                  <div className="cc-appr-actions">
                    <button className="cc-btn cc-btn-danger" onClick={() => resolveApproval(appr.command_id, 'deny')}>
                      Deny Action
                    </button>
                    <button className="cc-btn cc-btn-primary" onClick={() => resolveApproval(appr.command_id, 'approve')}>
                      Approve & Execute
                    </button>
                  </div>
                </div>
              ))}

              {/* Thinking / Working Indicator */}
              {isSending && (
                <div className="cc-message-row cortex">
                  <div className="cc-avatar cortex">CX</div>
                  <div className="cc-bubble-wrap">
                    <div className="cc-bubble sending-bubble">
                      <span className="cc-spinner-dot" />
                      <span>Reasoning over capabilities and executing...</span>
                    </div>
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>

            {/* Quick Goal Suggestions */}
            <div className="cc-quick-suggestions">
              <button className="cc-sug-chip" onClick={() => sendMessage(null, 'change my display to 60 hertz')}>
                🖥️ Change display to 60 Hz
              </button>
              <button className="cc-sug-chip" onClick={() => sendMessage(null, 'turn on dark mode')}>
                🌙 Turn on dark mode
              </button>
              <button className="cc-sug-chip" onClick={() => sendMessage(null, 'open Settings and show me my battery usage')}>
                🔋 Battery usage
              </button>
              <button className="cc-sug-chip" onClick={() => sendMessage(null, 'find the Bluetooth page')}>
                📶 Bluetooth page
              </button>
            </div>

            {/* Floating Glassmorphism Input Dock */}
            <div className="cc-input-dock">
              <form className="cc-input-form" onSubmit={sendMessage}>
                <textarea
                  rows={1}
                  value={inputText}
                  onChange={(e) => setInputText(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder="Ask Cortex or state a high-level goal... (Enter to send)"
                />
                <button
                  type="button"
                  className={`cc-btn-mic ${isVoiceActive ? 'active' : ''}`}
                  onClick={() => setIsVoiceActive(!isVoiceActive)}
                  title={isVoiceActive ? 'Voice listening active' : 'Simulate voice speech'}
                >
                  🎤
                </button>
                <button type="submit" className="cc-btn-send" disabled={isSending || !inputText.trim()}>
                  <span>Send</span>
                  <span>→</span>
                </button>
              </form>
            </div>
          </div>
        </main>

        {/* ── Slide-Out Secondary Operations Drawer ─────────────────────── */}
        <aside className={`cc-drawer ${activeDrawer ? 'open' : ''}`}>
          {activeDrawer && (
            <div className="cc-drawer-content">
              {/* Drawer Header */}
              <div className="cc-drawer-header">
                <div className="cc-drawer-title-wrap">
                  <h3 className="cc-drawer-title">{activeDrawer.replace('_', ' ').toUpperCase()}</h3>
                  <span className="cc-drawer-sub">Autonomous Operating Plane</span>
                </div>
                <button className="cc-drawer-close" onClick={() => setActiveDrawer(null)} title="Close drawer">
                  ✕
                </button>
              </div>

              {/* Drawer Surface: Tasks & Goals */}
              {activeDrawer === 'tasks' && (
                <div className="cc-drawer-section">
                  <div className="cc-section-toolbar">
                    <span className="cc-count-label">{tasks.length} Active Goals</span>
                    <button className="cc-btn cc-btn-secondary" onClick={fetchCanonicalState}>↻ Sync</button>
                  </div>
                  <div className="cc-cards-stack">
                    {tasks.map((t) => (
                      <div key={t.id} className="cc-panel-card">
                        <div className="cc-card-top">
                          <strong>{t.title}</strong>
                          <span className={`cc-status-pill ${t.status === 'COMPLETED' ? 'success' : t.status === 'RUNNING' || t.status === 'ACTIVE' ? 'warning' : 'danger'}`}>
                            {t.status}
                          </span>
                        </div>
                        <div className="cc-kv-row"><span className="k">Executor:</span><span className="v">{t.executor}</span></div>
                        <div className="cc-kv-row"><span className="k">Step:</span><span className="v">{t.current_step || 'In progress'}</span></div>
                        {t.waiting_reason && (
                          <div className="cc-kv-row"><span className="k">Waiting:</span><span className="v warning">{t.waiting_reason}</span></div>
                        )}
                        <div className="cc-progress-bar">
                          <div className="cc-progress-fill" style={{ width: `${Math.round((t.progress || 0) * 100)}%` }} />
                        </div>
                        <div className="cc-card-foot">
                          <small>{t.created_at ? new Date(t.created_at).toLocaleTimeString() : ''}</small>
                          {t.status !== 'COMPLETED' && t.status !== 'CANCELLED' && (
                            <button className="cc-btn cc-btn-danger sm" onClick={() => stopTask(t.id)}>Cancel</button>
                          )}
                        </div>
                      </div>
                    ))}
                    {tasks.length === 0 && (
                      <div className="cc-empty-state">No autonomous tasks active.</div>
                    )}
                  </div>
                </div>
              )}

              {/* Drawer Surface: Devices / Executors */}
              {activeDrawer === 'devices' && (
                <div className="cc-drawer-section">
                  <div className="cc-section-toolbar">
                    <span className="cc-count-label">{devices.length} Nodes Registered</span>
                    <button className="cc-btn cc-btn-secondary" onClick={fetchCanonicalState}>↻ Sync</button>
                  </div>
                  <div className="cc-cards-stack">
                    {devices.map((d) => (
                      <div key={d.device_id} className="cc-panel-card">
                        <div className="cc-card-top">
                          <strong>{d.display_name || d.device_id}</strong>
                          <span className={`cc-status-pill ${d.online ? 'success' : 'danger'}`}>
                            {d.online ? '● ONLINE' : '○ OFFLINE'}
                          </span>
                        </div>
                        <div className="cc-kv-row"><span className="k">Role:</span><span className="v">{d.role}</span></div>
                        <div className="cc-kv-row"><span className="k">Platform:</span><span className="v">{d.platform}</span></div>
                        <div className="cc-kv-row"><span className="k">Trust:</span><span className="v">{d.trust_level}</span></div>
                        <div className="cc-tags-block">
                          <small>Capabilities ({d.capabilities ? d.capabilities.length : 0}):</small>
                          <div className="cc-tag-list">
                            {(d.capabilities || []).slice(0, 8).map((c) => (
                              <span key={c} className="cc-tag">{c}</span>
                            ))}
                          </div>
                        </div>
                        {d.device_id !== 'asus' && d.online && (
                          <div className="cc-card-foot right">
                            <button className="cc-btn cc-btn-danger sm" onClick={() => disconnectDevice(d.device_id)}>Disconnect</button>
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Drawer Surface: Approvals */}
              {activeDrawer === 'approvals' && (
                <div className="cc-drawer-section">
                  <div className="cc-section-toolbar">
                    <span className="cc-count-label">{approvals.length} Pending Actions</span>
                    <button className="cc-btn cc-btn-secondary" onClick={fetchCanonicalState}>↻ Sync</button>
                  </div>
                  <div className="cc-cards-stack">
                    {approvals.map((appr) => (
                      <div key={appr.command_id} className="cc-panel-card alert-card">
                        <div className="cc-card-top">
                          <strong>{appr.action_summary}</strong>
                          <span className="cc-status-pill danger">TIER {appr.tier || 3}</span>
                        </div>
                        <p className="cc-card-desc">{appr.privacy_risk}</p>
                        <div className="cc-kv-row"><span className="k">Target:</span><span className="v">{appr.target_device}</span></div>
                        <div className="cc-kv-row"><span className="k">Data Leaves Local:</span><span className="v">{appr.data_leaves_local ? 'YES' : 'NO'}</span></div>
                        <div className="cc-card-actions">
                          <button className="cc-btn cc-btn-danger" onClick={() => resolveApproval(appr.command_id, 'deny')}>Deny</button>
                          <button className="cc-btn cc-btn-primary" onClick={() => resolveApproval(appr.command_id, 'approve')}>Approve</button>
                        </div>
                      </div>
                    ))}
                    {approvals.length === 0 && (
                      <div className="cc-empty-state">✓ No pending approvals. Autonomous execution within nominal boundaries.</div>
                    )}
                  </div>
                </div>
              )}

              {/* Drawer Surface: Transfers */}
              {activeDrawer === 'transfers' && (
                <div className="cc-drawer-section">
                  <div className="cc-section-toolbar">
                    <span className="cc-count-label">{transfers.length} Sessions</span>
                    <button className="cc-btn cc-btn-secondary" onClick={fetchCanonicalState}>↻ Sync</button>
                  </div>
                  <div className="cc-cards-stack">
                    {transfers.map((xfer) => (
                      <div key={xfer.transfer_id} className="cc-panel-card">
                        <div className="cc-card-top">
                          <strong>{xfer.filename}</strong>
                          <span className={`cc-status-pill ${xfer.state === 'COMPLETED' ? 'success' : xfer.state === 'ACTIVE' ? 'warning' : 'danger'}`}>
                            {xfer.state}
                          </span>
                        </div>
                        <div className="cc-kv-row"><span className="k">Route:</span><span className="v">{xfer.source} → {xfer.destination}</span></div>
                        <div className="cc-kv-row"><span className="k">Transport:</span><span className="v">{xfer.transport}</span></div>
                        <div className="cc-kv-row"><span className="k">Bytes:</span><span className="v">{xfer.bytes_transferred} / {xfer.total_bytes} B</span></div>
                        <div className="cc-progress-bar">
                          <div className="cc-progress-fill" style={{ width: `${xfer.percent || 0}%` }} />
                        </div>
                        <div className="cc-card-foot">
                          <small>{xfer.percent || 0}% done</small>
                          {xfer.state !== 'COMPLETED' && xfer.state !== 'CANCELLED' && (
                            <button className="cc-btn cc-btn-danger sm" onClick={() => cancelTransfer(xfer.transfer_id)}>Cancel</button>
                          )}
                          {xfer.resumable && xfer.state === 'CANCELLED' && (
                            <button className="cc-btn cc-btn-primary sm" onClick={() => resumeTransfer(xfer.transfer_id)}>Resume</button>
                          )}
                        </div>
                      </div>
                    ))}
                    {transfers.length === 0 && (
                      <div className="cc-empty-state">No cross-device transfer sessions recorded.</div>
                    )}
                  </div>
                </div>
              )}

              {/* Drawer Surface: System & Health */}
              {activeDrawer === 'system_health' && (
                <div className="cc-drawer-section">
                  <div className="cc-section-toolbar">
                    <span className="cc-count-label">System Health Matrix</span>
                    <button className="cc-btn cc-btn-secondary" onClick={fetchCanonicalState}>↻ Sync</button>
                  </div>
                  <div className="cc-cards-stack">
                    <div className="cc-panel-card">
                      <div className="cc-card-top">
                        <strong>LLM Providers</strong>
                        <span className="cc-status-pill success">HEALTHY</span>
                      </div>
                      {(systemHealth.providers || []).map((p, i) => (
                        <div key={i} className="cc-kv-row">
                          <span className="k">{p.name || 'Provider'}</span>
                          <span className="v">{p.status || 'Active'} ({p.latency_ms || 24}ms)</span>
                        </div>
                      ))}
                    </div>
                    <div className="cc-panel-card">
                      <div className="cc-card-top">
                        <strong>Available Capabilities</strong>
                        <span className="cc-status-pill success">{systemHealth.capabilities ? systemHealth.capabilities.length : 14} LOADED</span>
                      </div>
                      <div className="cc-tag-list" style={{ marginTop: 8 }}>
                        {(systemHealth.capabilities || []).map((c) => (
                          <span key={c} className="cc-tag">{c}</span>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              )}

              {/* Drawer Surface: World State */}
              {activeDrawer === 'world_state' && (
                <div className="cc-drawer-section">
                  <div className="cc-section-toolbar">
                    <span className="cc-count-label">Situational Awareness</span>
                    <button className="cc-btn cc-btn-secondary" onClick={fetchCanonicalState}>↻ Sync</button>
                  </div>
                  <div className="cc-cards-stack">
                    <div className="cc-panel-card">
                      <div className="cc-card-top">
                        <strong>Sensory Grounding</strong>
                        <span className="cc-status-pill success">ACTIVE</span>
                      </div>
                      <div className="cc-kv-row"><span className="k">Foreground App:</span><span className="v">{worldState.summary?.foreground_app || 'Settings / Desktop'}</span></div>
                      <div className="cc-kv-row"><span className="k">User Location:</span><span className="v">{worldState.summary?.user_location || 'Office / Desktop'}</span></div>
                      <div className="cc-kv-row"><span className="k">Audio Input:</span><span className="v">{isVoiceActive ? 'Listening (Mic Active)' : 'Standby'}</span></div>
                      <div className="cc-kv-row"><span className="k">Autonomy State:</span><span className="v">{autonomyEnabled ? 'Nominal' : 'Paused'}</span></div>
                    </div>
                  </div>
                </div>
              )}

              {/* Drawer Surface: Privacy Policy */}
              {activeDrawer === 'privacy' && (
                <div className="cc-drawer-section">
                  <div className="cc-section-toolbar">
                    <span className="cc-count-label">Phase A7 Privacy Gating</span>
                    <button className="cc-btn cc-btn-secondary" onClick={fetchCanonicalState}>↻ Sync</button>
                  </div>
                  <div className="cc-cards-stack">
                    <div className="cc-panel-card">
                      <div className="cc-card-top">
                        <strong>Data Routing Rules</strong>
                        <span className="cc-status-pill success">ENFORCED</span>
                      </div>
                      <div className="cc-kv-row"><span className="k">Tier 1:</span><span className="v">Read-only local execution</span></div>
                      <div className="cc-kv-row"><span className="k">Tier 2:</span><span className="v">Non-destructive system actions</span></div>
                      <div className="cc-kv-row"><span className="k">Tier 3:</span><span className="v warning">Mandatory confirmation gate</span></div>
                      <div className="cc-kv-row"><span className="k">Zero Secret Leakage:</span><span className="v">Credential masking active</span></div>
                    </div>
                  </div>
                </div>
              )}
            </div>
          )}
        </aside>
      </div>
    </div>
  )
}
