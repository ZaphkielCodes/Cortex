// Shared registry view for the browser dashboard. Provider values are text, never HTML.
(function (root) {
    function render(list, integrations, document) {
        list.replaceChildren();
        const text = (parent, tag, value) => {
            const child = document.createElement(tag);
            child.textContent = value;
            parent.appendChild(child);
            return child;
        };
        if (!integrations.length) text(list, 'p', 'No integration state reported.');
        for (const provider of integrations) {
            const card = document.createElement('div');
            card.className = 'card';
            text(card, 'h3', provider.name || provider.provider_id || 'Unknown integration');
            text(card, 'p', `Authorization: ${provider.auth_state || 'UNCONFIGURED'}`);
            text(card, 'p', `Service health: ${provider.health?.state || 'unknown'}`);
            const accounts = provider.accounts || [];
            if (!accounts.length) text(card, 'p', 'No authorized account reported.');
            for (const account of accounts) {
                text(card, 'p', `Account: ${account.identity?.email || account.account_id || 'Unknown account'}${account.default ? ' (default)' : ''}`);
                text(card, 'p', `Account authorization: ${account.auth_state || 'UNAUTHORIZED'}`);
                const details = text(card, 'details', '');
                text(details, 'summary', `Granted scopes (${(account.scopes || []).length})`);
                for (const scope of account.scopes || []) text(details, 'p', scope);
                if (['gmail', 'calendar', 'contacts'].includes(provider.key)) {
                    const link = text(card, 'a', 'Review read-only authorization');
                    link.href = `/integrations/${encodeURIComponent(provider.key)}/connect?account_id=${encodeURIComponent(account.account_id)}`;
                    // /connect redirects to consent; /auth is a JSON endpoint.
                }
            }
            for (const cap of provider.capabilities || []) {
                text(card, 'p', `${cap.id}: ${cap.available ? 'Configured for use' : cap.reason || cap.auth_state || 'Unavailable'}`);
            }
            for (const action of (provider.recent_actions || []).slice(-5)) {
                text(card, 'p', `${action.capability}: ${action.status}${action.error_category ? ` (${action.error_category})` : ''}`);
            }
            list.appendChild(card);
        }
    }
    root.CortexIntegrations = { render };
})(typeof window === 'undefined' ? globalThis : window);
