"""A13 reconciles evidence on A5 DeviceHandles against the canonical registry.

No separate executor registry and no registration authority for model output.
Server-side policy and execution-time checks remain authoritative.
"""
import logging
import time
from datetime import datetime, timezone
from shared.discovery import AdvertisementBatch, describe, schema_hash

log = logging.getLogger('cortex.discovery')
KEY = 'capability_discovery'


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def validate_advertisement(device_id, batch):
    """Preflight a handshake without touching an existing authenticated handle."""
    if batch is None: return  # Legacy names are filtered by reconcile, never registered as new tools.
    if batch.device_id != device_id: raise ValueError('Advertisement identity mismatch')
    from .capability_registry import CanonicalCapabilityRegistry
    canonical=CanonicalCapabilityRegistry.get_instance()
    for ad in batch.capabilities:
        cap=canonical.get_capability(ad.capability_id)
        if not cap or cap.target_type!='device': raise ValueError('Unregistered device capability')
        expected=describe(cap,ad.evidence)
        if (ad.permission_tier,ad.risk,ad.required_permissions,ad.privacy_class)!=(expected.permission_tier,expected.risk,expected.required_permissions,expected.privacy_class):
            raise ValueError('Advertised policy cannot override canonical policy')


def reconcile(registry, device_id, batch, *, legacy_names=None):
    """Only call on the authenticated current A5 session. Commit atomically."""
    from .capability_registry import CanonicalCapabilityRegistry
    handle = registry.get_device_handle(device_id)
    if handle is None or not handle.authenticated or device_id not in registry._live_connections:
        raise ValueError('Authenticated current session required')
    canonical = CanonicalCapabilityRegistry.get_instance()
    legacy = batch is None
    if legacy:
        records = []
        for name in sorted(set(legacy_names or [])):
            cap = canonical.get_capability(name)
            if cap and cap.target_type == 'device':
                records.append(describe(cap, 'legacy_report'))
        batch = AdvertisementBatch(device_id=device_id, capabilities=records)
        log.info('discovery_compatibility device=%s reason=legacy_name_list', device_id)
    validate_advertisement(device_id,batch)
    prior = handle.metadata.get(KEY)
    connection = id(registry._live_connections[device_id])
    if prior and prior['connection'] == connection and batch.generation < prior['generation']:
        raise ValueError('Stale capability generation')
    entries = {}
    for ad in batch.capabilities:
        cap = canonical.get_capability(ad.capability_id)
        if not cap or cap.target_type != 'device': raise ValueError('Unregistered device capability')
        expected = describe(cap, ad.evidence)
        if (ad.permission_tier,ad.risk,ad.required_permissions,ad.privacy_class) != (expected.permission_tier,expected.risk,expected.required_permissions,expected.privacy_class):
            raise ValueError('Advertised policy cannot override canonical policy')
        compatible = schema_hash(ad) == schema_hash(expected)
        safe_ad = ad.model_dump()
        # Retain incompatible support as presence/evidence, never arbitrary
        # provider schema payloads. Only canonical schemas cross read APIs.
        safe_ad.update(input_schema=expected.input_schema, output_schema=expected.output_schema)
        entries[cap.name] = {'advertisement': safe_ad, 'compatible': compatible,
            'schema_hash': schema_hash(ad), 'last_verified_at': None}
    if prior and prior['connection'] == connection and batch.generation == prior['generation']:
        old = {k: v['advertisement'] for k, v in prior['entries'].items()}
        if old != {k: v['advertisement'] for k, v in entries.items()}:
            raise ValueError('Changed capability set requires a new generation')
        for name in entries: entries[name]['last_verified_at'] = prior['entries'][name]['last_verified_at']
    handle.metadata[KEY] = {'entries': entries, 'connection': connection, 'generation': batch.generation,
        'expires_mono': time.monotonic()+batch.ttl_seconds, 'expires_at': time.time()+batch.ttl_seconds,
        'received_at': now_iso(), 'protocol_version': 0 if legacy else 1}
    handle.capabilities = [name for name, value in entries.items() if value['compatible'] and value['advertisement']['availability'] != 'unavailable']
    return handle.capabilities


def eligibility(registry, device_id, capability):
    """Technical eligibility only. Never confers permission or task privacy approval."""
    handle = registry.get_device_handle(device_id)
    saved = handle.metadata.get(KEY) if handle else None
    if not saved: return None  # Historical/test handles retain existing A5 behavior.
    if not registry.is_device_online(device_id): return 'DEVICE_OFFLINE'
    if saved['connection'] != id(registry._live_connections.get(device_id)): return 'SESSION_REPLACED'
    if time.monotonic() >= saved['expires_mono']: return 'ADVERTISEMENT_EXPIRED'
    entry = saved['entries'].get(capability)
    if not entry: return 'NOT_ADVERTISED'
    if not entry['compatible']: return 'SCHEMA_INCOMPATIBLE'
    from .capability_registry import CanonicalCapabilityRegistry
    cap = CanonicalCapabilityRegistry.get_instance()._capabilities.get(capability)
    if not cap or not cap.availability: return 'CAPABILITY_REVOKED'
    expected = describe(cap)
    if schema_hash(expected) != entry['schema_hash']: return 'SCHEMA_INCOMPATIBLE'
    ad = entry['advertisement']
    if (ad['permission_tier'],ad['risk'],ad['required_permissions'],ad['privacy_class']) != (expected.permission_tier,expected.risk,expected.required_permissions,expected.privacy_class):
        return 'POLICY_CHANGED'
    if entry['advertisement']['availability'] == 'unavailable': return 'EXECUTOR_UNAVAILABLE'
    return 'ELIGIBLE'


def ensure_dispatch(device_id, capability):
    from .executor_registry import ExecutorRegistry
    result = eligibility(ExecutorRegistry.get_instance(), device_id, capability)
    if result not in (None, 'ELIGIBLE'): raise ValueError('Capability discovery rejected execution: '+result)


async def guard_dispatch(device_id, capability):
    ensure_dispatch(device_id, capability)
    from .database import get_device_permission
    if await get_device_permission(device_id, capability) == 'deny':
        raise ValueError('Capability permission was revoked')
    from .executor_registry import ExecutorRegistry
    from .capability_registry import CanonicalCapabilityRegistry
    handle = ExecutorRegistry.get_instance().get_device_handle(device_id)
    cap = CanonicalCapabilityRegistry.get_instance().get_capability(capability)
    if handle and handle.metadata.get(KEY) and cap and not handle.is_authorized_for(capability,tier=cap.permission_tier,classification=cap.classification):
        raise ValueError('Executor trust policy rejects this capability')


def mark_verified(registry, device_id, capability):
    handle = registry.get_device_handle(device_id)
    saved = handle.metadata.get(KEY) if handle else None
    if saved and eligibility(registry, device_id, capability) == 'ELIGIBLE':
        saved['entries'][capability]['last_verified_at'] = now_iso()


async def record_verified_result(device_id, message):
    if message.status != 'success': return
    from .database import get_db
    async with get_db() as db:
        async with db.execute('SELECT 1 FROM command_history WHERE command_id=? AND command=? AND target_device=? AND status=?',
            (message.command_id,message.command,device_id,'dispatched')) as cursor:
            matched=await cursor.fetchone()
    if matched:
        from .executor_registry import ExecutorRegistry
        mark_verified(ExecutorRegistry.get_instance(),device_id,message.command)


async def candidates(source, *, privacy_context=None):
    """Caller-scoped catalog; existing RBAC, trust, privacy and OAuth still apply."""
    from .capability_registry import CanonicalCapabilityRegistry
    from .executor_registry import ExecutorRegistry
    from .database import get_device
    from .permissions import check_permission, ROLE_PERMISSIONS
    from .privacy import PrivacyPolicyEngine, classify_task_privacy
    from .integrations.ecosystem import IntegrationRegistry
    from .integrations.definitions import AuthState
    caller = await get_device(source)
    if not caller: return []
    role = caller['role']; er = ExecutorRegistry.get_instance()
    catalog = CanonicalCapabilityRegistry.get_instance()
    rows = []
    for handle in er.list_device_handles():
        if source != handle.device_id and '*' not in ROLE_PERMISSIONS.get(role, {}).get('can_target', []): continue
        saved = handle.metadata.get(KEY)
        if not saved: continue  # DB history alone is not discovered support.
        for name, entry in saved['entries'].items():
            cap = catalog.get_capability(name)
            if not cap: continue
            reason = eligibility(er, handle.device_id, name)
            # Preview authorization independently of presence/liveness. Execution
            # callers always use the default support checks.
            perm = await check_permission(role, source, handle.device_id, name, include_support=False)
            trusted = handle.is_authorized_for(name, tier=cap.permission_tier, classification=cap.classification)
            private = PrivacyPolicyEngine.get_instance().evaluate_candidate(handle,
                classify_task_privacy(name, {}, privacy_context or {}), name)
            authorized = perm.allowed and trusted and private.is_allowed
            if reason == 'ELIGIBLE' and not authorized: reason = 'PERMISSION_OR_PRIVACY_DENIED'
            elif reason == 'ELIGIBLE' and perm.needs_confirmation: reason = 'APPROVAL_REQUIRED'
            executable = reason == 'ELIGIBLE'
            ad = entry['advertisement']
            rows.append(dict(capability_id=name, provider_id=handle.device_id, executor_location=handle.locality,
                version=ad['version'], protocol_version=saved['protocol_version'], input_schema=ad['input_schema'],
                output_schema=ad['output_schema'], present=True, available=eligibility(er,handle.device_id,name)=='ELIGIBLE',
                authorized=bool(authorized), executable=executable, reason=reason,
                state='APPROVAL_REQUIRED' if reason=='APPROVAL_REQUIRED' else 'DEGRADED' if executable and ad['availability']=='degraded' else
                    'EXECUTABLE' if executable else 'UNAUTHORIZED' if not authorized else 'UNAVAILABLE',
                health=ad['availability'], permission_tier=cap.permission_tier, risk=cap.risk,
                privacy=dict(locality=handle.locality, private_compute_allowed=handle.private_compute_allowed),
                last_verified_at=entry['last_verified_at'], expires_at=saved['expires_at'],
                evidence=ad['evidence'], schema_compatible=entry['compatible']))
    if role in {'admin', 'primary_admin'}:
        integrations = IntegrationRegistry.get_instance()
        await integrations.sync_connections()
        for cap in integrations.capabilities.values():
            accounts = integrations.accounts(cap.provider_id) or [None]
            for account in accounts:
                installed = cap.provider_id in integrations.providers
                configured = integrations.oauth_configured if account is None else True
                scopes = account is not None and integrations.scopes_satisfy(cap.requirement.scopes, account.scopes)
                authorized = bool(account and account.auth_state == AuthState.AUTHORIZED and scopes)
                health = integrations.health.get(cap.provider_id, {})
                healthy = health.get('cooldown_until', 0) <= time.time()
                perm = await check_permission(role,source,source,cap.capability_id)
                local_only = (privacy_context or {}).get('preferred_execution_scope') in {'local_only','private_compute'} or (privacy_context or {}).get('privacy_class') == 'HIGHLY_SENSITIVE'
                reason = ('DEFERRED' if cap.provider_id == 'google_drive' else 'NOT_INSTALLED' if not installed else
                    'NOT_CONFIGURED' if not configured else 'UNAUTHORIZED' if not authorized else
                    'PROVIDER_UNAVAILABLE' if not healthy else 'PRIVACY_DENIED' if local_only else
                    'PERMISSION_DENIED' if not perm.allowed else 'APPROVAL_REQUIRED' if perm.needs_confirmation else 'ELIGIBLE')
                # Opaque account alias; no addresses, identities, scopes or token metadata leak.
                import hashlib
                ref = hashlib.sha256((cap.provider_id+':'+account.account_id).encode()).hexdigest()[:16] if account else 'none'
                rows.append(dict(capability_id=cap.capability_id, provider_id=cap.provider_id, account_ref=ref,
                    executor_location='third_party_service', version='1.0', protocol_version=1,
                    input_schema=integrations._argument_schemas[cap.capability_id], output_schema=integrations._result_schemas[cap.capability_id],
                    present=True, installed=installed, configured=configured, authorized=authorized,
                    available=bool(installed and authorized and healthy and reason != 'DEFERRED'), executable=reason=='ELIGIBLE',
                    state='APPROVAL_REQUIRED' if reason=='APPROVAL_REQUIRED' else 'DEGRADED' if reason=='ELIGIBLE' and health.get('state')=='degraded' else
                        'EXECUTABLE' if reason=='ELIGIBLE' else 'UNAUTHORIZED' if not authorized else 'UNAVAILABLE',reason=reason,
                    health=health.get('state','unknown'), permission_tier=cap.permission_tier,
                    risk='low' if cap.effect.value=='READ' else 'high', privacy={'data_class':cap.data_class},
                    last_verified_at=account.last_verified if account else None,expires_at=None,evidence='account_scopes_and_adapter'))
    return rows
