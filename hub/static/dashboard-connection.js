// Cookie-authenticated control channel; no browser-held device secret is required.
(function (root) {
    function create({ WebSocket, url, onEvent, onConnected, onAuthRequired,
        checkSession, setTimer = setTimeout, clearTimer = clearTimeout }) {
        let socket = null, timer = null, stopped = false, failures = 0;
        function stop() {
            stopped = true;
            if (timer !== null) clearTimer(timer);
            timer = null;
            const old = socket;
            socket = null;
            if (old) old.close();
        }
        function authRequired() { stop(); onAuthRequired(); }
        function connect() {
            if (stopped || socket) return;
            const current = new WebSocket(url);
            socket = current;
            current.onopen = () => {
                if (socket !== current || stopped) return;
                failures = 0;
                onConnected();
            };
            current.onmessage = event => {
                if (socket !== current || stopped) return;
                try { onEvent(JSON.parse(event.data)); } catch (_) { /* Ignore invalid frames. */ }
            };
            current.onclose = async event => {
                if (socket !== current || stopped) return;
                socket = null;
                if ([1008, 4001, 4002, 4003, 4004, 4401, 4403].includes(event.code)) {
                    authRequired(); return;
                }
                // A rejected handshake can appear as 1006 in browsers. Check the
                // authenticated HTTP endpoint before scheduling another connection.
                const valid = await checkSession().catch(() => null);
                if (stopped || socket) return;
                if (valid === false) { authRequired(); return; }
                const delay = Math.min(30000, 1000 * 2 ** Math.min(failures++, 5));
                timer = setTimer(() => { timer = null; connect(); }, delay);
            };
        }
        return { start: connect, stop, send(data) {
            if (!socket || socket.readyState !== 1) return false;
            socket.send(JSON.stringify(data)); return true;
        } };
    }
    root.CortexDashboardConnection = { create };
})(typeof window === 'undefined' ? globalThis : window);
