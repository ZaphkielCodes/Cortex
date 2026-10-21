"""App-independent text-field selection and evidence checks."""
from .ui_models import UIElement, UIObservation


def is_editable(element: UIElement) -> bool:
    meta = element.metadata
    if not element.enabled or meta.get('is_read_only') or meta.get('is_offscreen'):
        return False
    if meta.get('editable') is False:
        return False
    return meta.get('editable') is True or element.role.lower() in {
        'input', 'edit', 'textbox', 'searchbox', 'textarea', 'textentry'}


def resolve_text_target(observation: UIObservation, target=None):
    """Never fall through from a missing target to whichever field has focus."""
    candidates = observation.elements
    if target:
        exact_id = [e for e in candidates if e.element_id == target]
        candidates = exact_id or [e for e in candidates if e.label.casefold() == str(target).casefold()]
    else:
        candidates = [e for e in candidates if e.metadata.get('has_keyboard_focus')]
    if len(candidates) != 1:
        return None, 'ambiguous_target' if candidates else 'target_not_found'
    if not is_editable(candidates[0]):
        return None, 'target_not_editable'
    return candidates[0], None


def corresponding_element(before: UIObservation, after: UIObservation, element: UIElement):
    if before.device_id != after.device_id or before.window_id != after.window_id:
        return None
    if before.foreground_app != after.foreground_app:
        return None
    if before.pid and before.pid != after.pid:
        return None
    runtime_id = element.metadata.get('runtime_id')
    automation_id = element.metadata.get('automation_id')
    if runtime_id:
        matches = [e for e in after.elements if e.metadata.get('runtime_id') == runtime_id]
    elif automation_id:
        matches = [e for e in after.elements if e.metadata.get('automation_id') == automation_id
                   and e.role == element.role]
    else:
        # Snapshot-local el_N IDs are not stable across tree enumeration.
        matches = [e for e in after.elements if e.role == element.role and e.label == element.label
                   and e.bounds == element.bounds]
    return matches[0] if len(matches) == 1 else None


def verify_text_entry(before: UIObservation, after: UIObservation, arguments):
    if not before or not after:
        return False
    element, error = resolve_text_target(before, arguments.get('target'))
    if error or arguments.get('is_credential') or arguments.get('secret_id') or element.metadata.get('is_password'):
        return False
    current = corresponding_element(before, after, element)
    text = arguments.get('text', arguments.get('value', ''))
    if current is None or not isinstance(text, str) or not text:
        return False
    if arguments.get('clear_existing'):
        return current.text == text
    return text in current.text
