import { humanStatus } from "./control-client.js";

export default function IntegrationStatus({ integrations, connected, busy, setDefault }) {
  if (!integrations.length) return <p>Integration state has not been received.</p>;
  return <div>{integrations.map(provider => <article key={provider.integration_id || provider.key}>
    <div className="data-row"><strong>{provider.name}</strong><span>{humanStatus(provider.auth_state || provider.status)}</span></div>
    <p>Service health: {humanStatus(provider.health?.state || "unknown")}</p>
    {!provider.accounts?.length && <p>No authorized account reported. Google services require explicit setup and consent. Maps also requires an enabled project and billing.</p>}
    {(provider.accounts || []).map(account => <div className="data-row" key={account.account_id}>
      <div><strong>{account.identity?.email || account.account_id}</strong><small>{humanStatus(account.auth_state)}{account.default ? " · Default account" : ""}</small>
        <details><summary>Granted permissions ({account.scopes.length})</summary><ul>{account.scopes.map(scope => <li key={scope} style={{ overflowWrap: "anywhere" }}>{scope}</li>)}</ul></details>
      </div>
      {!account.default && <button className="control-button" disabled={!connected || !!busy} onClick={() => setDefault(provider.provider_id, account.account_id)}>Use as default</button>}
    </div>)}
    <details><summary>Available actions and consent requirements</summary><ul>{(provider.capabilities || []).map(cap => <li key={cap.id}>{cap.id}: {cap.available ? "Configured for use" : humanStatus(cap.reason || cap.auth_state)}{cap.required_scopes?.length ? ` · ${cap.required_scopes.join(", ")}` : ""}</li>)}</ul></details>
    {(provider.recent_actions || []).slice(-5).map((action, index) => <p key={index}>{action.capability} · {humanStatus(action.status)}{action.error_category ? ` · ${humanStatus(action.error_category)}` : ""}</p>)}
  </article>)}</div>;
}
