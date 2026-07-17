from __future__ import annotations

import json
import subprocess
from pathlib import Path


PLUGIN = Path(__file__).parents[3] / "tracing" / "opencode" / "plugin" / "arize-tracing.ts"


def _run_node(script: str) -> dict:
    result = subprocess.run(
        ["node", "--experimental-transform-types", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_sdk_error_result_does_not_fetch_or_forward_root_transcript() -> None:
    result = _run_node(
        f"""
        import {{ ArizeTracing }} from {json.dumps(PLUGIN.as_uri())};
        let messagesCalls = 0;
        const hooks = await ArizeTracing({{
          client: {{ session: {{
            get: async () => ({{ error: {{ message: 'unavailable' }}, data: undefined }}),
            messages: async () => {{ messagesCalls += 1; return {{ data: [] }}; }},
          }} }},
        }});
        await hooks.event({{ event: {{
          type: 'message.updated',
          properties: {{ info: {{ role: 'assistant', time: {{ completed: 1 }}, sessionID: 'ses_root' }} }},
        }} }});
        console.log(JSON.stringify({{ messagesCalls }}));
        """
    )
    assert result == {"messagesCalls": 0}
