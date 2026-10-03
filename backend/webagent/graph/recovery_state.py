"""Read application references before constructing live execution clients."""
from ..errors import BusinessError
from .models import GraphSnapshot

_NODES = frozenset(('reconcile', 'observe', 'decide', 'dispatch', 'confirm', 'verify',
                    'aggregate', 'recover', 'prepare_wait', 'wait', 'stopped'))


async def load_saved_graph(checkpointer, run_id):
    try:
        saved = await checkpointer.aget_tuple({'configurable': {'thread_id': run_id}})
    except Exception:
        raise BusinessError('STATE_CONFLICT', 'Saved graph cannot be decoded', status=409,
                            field='graph_state_invalid') from None
    if saved is None:
        return None
    channels = saved.checkpoint.get('channel_values') if type(saved.checkpoint) is dict else None
    if type(channels) is not dict:
        raise BusinessError('STATE_CONFLICT', 'Saved graph channels are unavailable', status=409,
                            field='graph_state_invalid')
    fields = set(GraphSnapshot.model_fields)
    application, initial = {}, None
    for key, value in channels.items():
        if key in fields:
            application[key] = value
        elif key == '__start__':
            # The first framework save can precede START materializing the
            # application channels. Even its input must remain reference-only.
            try:
                initial = GraphSnapshot.model_validate(value).model_dump(mode='json')
            except (TypeError, ValueError):
                raise BusinessError('STATE_CONFLICT', 'Saved initial graph state is invalid',
                                    status=409, field='graph_state_invalid') from None
        elif type(key) is str and key.startswith('branch:to:') and key.removeprefix('branch:to:') in _NODES and value is None:
            continue
        else:
            raise BusinessError('STATE_CONFLICT', 'Saved graph contains unsupported channels',
                                status=409, field='graph_state_invalid')
    if not application and initial is None:
        raise BusinessError('STATE_CONFLICT', 'Saved graph application state is missing', status=409,
                            field='graph_state_invalid')
    return application or initial
