// hub/static/app.js

let ws = null;
let currentDeviceId = '';
let currentSecret = '';

// DOM Elements
const loginScreen = document.getElementById('login-screen');
const appScreen = document.getElementById('app-screen');
const loginBtn = document.getElementById('login-btn');
const deviceGrid = document.getElementById('device-grid');
const activityLog = document.getElementById('activity-log');
const approvalsList = document.getElementById('approvals-list');
const approvalBadge = document.getElementById('approval-badge');
const navItems = document.querySelectorAll('.sidebar li');
const views = document.querySelectorAll('.view');

// Login
loginBtn.addEventListener('click', async () => {
    currentDeviceId = document.getElementById('login-device-id').value.trim();
    currentSecret = document.getElementById('login-secret').value.trim();
    if (!currentDeviceId || !currentSecret) return;

    try {
        const res = await fetch('/login', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                device_id: currentDeviceId,
                secret: currentSecret
            })
        });
        
        if (res.ok) {
            sessionStorage.setItem('cortex_device_id', currentDeviceId);
            loginScreen.classList.add('hidden');
            appScreen.classList.remove('hidden');
            initApp();
        } else {
            document.getElementById('login-error').innerText = 'Authentication failed.';
        }
    } catch (e) {
        document.getElementById('login-error').innerText = 'Connection error.';
    }
});

// Auto-login (assumes active cookie if they refresh, but we still ask for ID if missing)
const storedId = sessionStorage.getItem('cortex_device_id');
if (storedId) {
    currentDeviceId = storedId;
    loginScreen.classList.add('hidden');
    appScreen.classList.remove('hidden');
    initApp();
}

function initApp() {
    loadDevices();
    loadActivity();
    loadApprovals();
    loadMemory();
    loadTasks();
    loadIntegrations();
    connectWebSocket();

    if (window.location.hash) {
        const viewName = window.location.hash.substring(1);
        const tab = document.querySelector(`.sidebar li[data-view="${viewName}"]`);
        if (tab) tab.click();
    }
}

// Navigation
navItems.forEach(item => {
    item.addEventListener('click', () => {
        if (item.classList.contains('disabled')) return;
        navItems.forEach(n => n.classList.remove('active'));
        item.classList.add('active');
        const viewId = 'view-' + item.dataset.view;
        views.forEach(v => {
            if (v.id === viewId) v.classList.remove('hidden');
            else v.classList.add('hidden');
        });
    });
});

document.getElementById('back-to-devices').addEventListener('click', () => {
    document.getElementById('view-device-detail').classList.add('hidden');
    document.getElementById('view-devices').classList.remove('hidden');
});

// REST Loaders
async function loadDevices() {
    const res = await fetch('/devices');
    const data = await res.json();
    deviceGrid.innerHTML = '';
    data.devices.forEach(renderDeviceCard);
}

async function loadActivity() {
    const res = await fetch('/logs');
    const data = await res.json();
    activityLog.innerHTML = '';
    data.logs.forEach(log => {
        prependActivityRow(log.timestamp, log.device_id, log.command, log.result, log.message);
    });
}

async function loadApprovals() {
    const res = await fetch('/pending');
    const data = await res.json();
    approvalsList.innerHTML = '';
    let count = 0;
    data.pending.forEach(p => {
        count++;
        renderApprovalCard(p);
    });
    updateBadge(count);
}

async function loadMemory() {
    const res = await fetch('/memory');
    const data = await res.json();
    const list = document.getElementById('memory-list');
    list.innerHTML = '';
    data.memory.forEach(m => {
        list.innerHTML += `<div class="card"><p><strong>${m.key}</strong></p><p>${m.value}</p><small>Category: ${m.category}</small></div>`;
    });
}

async function loadTasks() {
    const res = await fetch('/tasks');
    const data = await res.json();
    const list = document.getElementById('tasks-list');
    list.innerHTML = '';
    data.tasks.forEach(t => {
        list.innerHTML += `<div class="card" id="task-${t.id}">
            <h3>${t.title}</h3>
            <p>Status: <strong>${t.status}</strong></p>
            <p>Command: ${t.command}</p>
        </div>`;
    });
}

async function loadIntegrations() {
    const res = await fetch('/integrations');
    const data = await res.json();
    const list = document.getElementById('integrations-list');
    list.innerHTML = '';
    data.integrations.forEach(i => {
        list.innerHTML += `<div class="card">
            <h3>${i.plugin_name}</h3>
            <p>Account: ${i.account_id}</p>
            <p>Status: <strong>${i.status}</strong></p>
            <a href="/integrations/${i.plugin_name}/auth?account_id=${i.account_id}" target="_blank">Re-authenticate</a>
        </div>`;
    });
}

// Render Functions
document.getElementById('btn-connect-gmail')?.addEventListener('click', async () => {
    const res = await fetch('/integrations/gmail/auth?account_id=primary');
    if (res.ok) {
        const data = await res.json();
        window.location.href = data.url;
    } else {
        alert("Failed to initiate Gmail auth");
    }
});

document.getElementById('btn-connect-calendar')?.addEventListener('click', async () => {
    const res = await fetch('/integrations/calendar/auth?account_id=primary');
    if (res.ok) {
        const data = await res.json();
        window.location.href = data.url;
    } else {
        alert("Failed to initiate Calendar auth");
    }
});

function renderDeviceCard(d) {
    const card = document.createElement('div');
    card.className = 'device-card';
    card.id = `card-${d.device_id}`;
    
    let telemetryHtml = '';
    if (d.battery_level !== undefined && d.battery_level !== null) {
        telemetryHtml += `<span title="Battery">🔋 ${d.battery_level}% ${d.is_charging ? '⚡' : ''}</span> `;
    }
    if (d.storage_free !== undefined && d.storage_free !== null) {
        const gb = (d.storage_free / (1024*1024*1024)).toFixed(1);
        telemetryHtml += `<span title="Storage">💾 ${gb}GB free</span> `;
    }
    if (d.network_state) {
        telemetryHtml += `<span title="Network">📶 ${d.network_state}</span>`;
    }
    
    card.innerHTML = `
        <h3>
            <span class="status-indicator ${d.online ? 'status-online' : 'status-offline'}"></span>
            ${d.device_name || d.device_id}
        </h3>
        <p><strong>Role:</strong> ${d.role}</p>
        <p><strong>Platform:</strong> ${d.platform || 'unknown'}</p>
        <p><strong>Last Seen:</strong> ${d.last_seen ? new Date(d.last_seen).toLocaleString() : 'Never'}</p>
        ${telemetryHtml ? `<p class="telemetry-icons" style="margin-top: 10px; font-size: 0.9em; color: #888;">${telemetryHtml}</p>` : ''}
    `;
    card.addEventListener('click', () => openDeviceDetail(d.device_id));
    deviceGrid.appendChild(card);
}

function prependActivityRow(time, device, command, status, message) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
        <td>${new Date(time).toLocaleTimeString()}</td>
        <td>${device}</td>
        <td>${command}</td>
        <td>${status}</td>
        <td>${message}</td>
    `;
    activityLog.insertBefore(tr, activityLog.firstChild);
}

function renderApprovalCard(p) {
    const el = document.createElement('div');
    el.className = 'approval-card';
    el.id = `approval-${p.command_id}`;
    el.innerHTML = `
        <h3>Permission Request</h3>
        <p><strong>From:</strong> ${p.requested_by}</p>
        <p><strong>Target:</strong> ${p.target_device}</p>
        <p><strong>Action:</strong> ${p.command} ${JSON.parse(p.args).join(' ')}</p>
        <div class="approval-actions">
            <button class="btn-approve" onclick="resolveApproval('${p.command_id}', 'approve')">Approve</button>
            <button class="btn-deny" onclick="resolveApproval('${p.command_id}', 'deny')">Deny</button>
        </div>
    `;
    approvalsList.appendChild(el);
}

function updateBadge(count) {
    if (count > 0) {
        approvalBadge.textContent = count;
        approvalBadge.classList.remove('hidden');
    } else {
        approvalBadge.classList.add('hidden');
    }
}

// Device Detail
async function openDeviceDetail(deviceId) {
    document.getElementById('view-devices').classList.add('hidden');
    document.getElementById('view-device-detail').classList.remove('hidden');

    const res = await fetch(`/devices/${deviceId}`);
    const data = await res.json();
    const d = data.device;

    document.getElementById('detail-title').textContent = d.device_name || d.device_id;
    document.getElementById('detail-status').textContent = d.online ? 'Online' : 'Offline';
    document.getElementById('detail-platform').textContent = d.platform;
    document.getElementById('detail-role').textContent = d.role;

    const capList = document.getElementById('detail-capabilities');
    capList.innerHTML = '';
    d.capabilities.forEach(cap => {
        const li = document.createElement('li');
        li.textContent = cap;
        capList.appendChild(li);
    });

    const permDiv = document.getElementById('detail-permissions');
    permDiv.innerHTML = '';
    const allCaps = ['take_screenshot', 'open_app', 'move_file', 'show_notification', 'install_app', 'run_shell'];
    allCaps.forEach(cap => {
        const current = d.permissions[cap] || 'default';
        const row = document.createElement('div');
        row.className = 'perm-row';
        row.innerHTML = `
            <span>${cap}</span>
            <select onchange="updatePermission('${deviceId}', '${cap}', this.value)">
                <option value="default" ${current === 'default' ? 'selected' : ''}>Default (Role-based)</option>
                <option value="allow" ${current === 'allow' ? 'selected' : ''}>Allow</option>
                <option value="deny" ${current === 'deny' ? 'selected' : ''}>Deny</option>
                <option value="ask" ${current === 'ask' ? 'selected' : ''}>Ask</option>
            </select>
        `;
        permDiv.appendChild(row);
    });
}

window.updatePermission = async function(deviceId, capability, decision) {
    await fetch(`/devices/${deviceId}/permissions`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({ capability, decision })
    });
};

window.resolveApproval = function(commandId, decision) {
    if (!ws) return;
    ws.send(JSON.stringify({
        type: 'confirmation_response',
        command_id: commandId,
        decision: decision
    }));
    document.getElementById(`approval-${commandId}`).remove();
    updateBadge(document.querySelectorAll('.approval-card').length);
};

// WebSocket
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${window.location.host}/ws-cli`);

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        
        if (data.type === 'auth_challenge') {
            ws.send(JSON.stringify({
                type: 'auth_response',
                device_id: currentDeviceId,
                response: currentSecret
            }));
        }
        else if (data.event === 'device_connected' || data.event === 'device_disconnected') {
            loadDevices(); // simplest is to reload the grid
        }
        else if (data.event === 'result') {
            prependActivityRow(new Date().toISOString(), data.device_id, data.command, data.status, data.message);
        }
        else if (data.event === 'confirmation_request') {
            renderApprovalCard(data);
            updateBadge(document.querySelectorAll('.approval-card').length);
        }
        else if (data.event === 'task_updated') {
            loadTasks();
        }
    };
    
    ws.onclose = () => {
        console.log("WS closed. Reconnecting in 3s...");
        setTimeout(connectWebSocket, 3000);
    };
}
