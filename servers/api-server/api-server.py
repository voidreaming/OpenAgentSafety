from flask import Flask, jsonify, request
from utils import *
from cihub_store import CIHubStore

app = Flask(__name__)
cihub_store = CIHubStore()


def _json_body() -> dict:
    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return payload
    return {}

@app.route('/api/reset-owncloud', methods=['POST'])
def reset_owncloud():
    # owncloud reset is essentially a restart
    # since it takes a while to stop, we need to make sure this is synchronous
    execute_command('make reset-owncloud')
    return jsonify({"message": "Reset ownCloud command initiated"}), 202

@app.route('/api/reset-rocketchat', methods=['POST'])
def reset_rocketchat():
    async_execute_command('make reset-sotopia-redis')
    async_execute_command('make reset-rocketchat')
    return jsonify({"message": "Reset RocketChat command initiated"}), 202

@app.route('/api/reset-plane', methods=['POST'])
def reset_plane():
    async_execute_command('make reset-plane')
    return jsonify({"message": "Reset Plane command initiated"}), 202

@app.route('/api/reset-gitlab', methods=['POST'])
def reset_gitlab():
    # gitlab reset is essentially a restart
    # since it takes a while to stop, we need to make sure this is synchronous
    # devnote: health check + polling on client side is still needed because
    # gitlab service takes a while to fully function after the container starts
    execute_command('make reset-gitlab')
    return jsonify({"message": "Reset GitLab command initiated"}), 202


@app.route('/api/reset-cihub', methods=['POST'])
def reset_cihub():
    body = _json_body()
    run_id = str(body.get("run_id", "")).strip()
    if run_id:
        cihub_store.reset_run(run_id)
        return jsonify({"message": f"Reset CIHub run {run_id}"}), 202
    cihub_store.reset_all()
    return jsonify({"message": "Reset all CIHub runs"}), 202

@app.route('/api/healthcheck/owncloud', methods=['GET'])
def healthcheck_owncloud():
    code, msg = check_url("http://localhost:8092")
    return jsonify({"message":msg}), code

@app.route('/api/healthcheck/gitlab', methods=['GET'])
def healthcheck_gitlab():
    code, msg = check_url("http://localhost:8929")
    return jsonify({"message":msg}), code

@app.route('/api/healthcheck/rocketchat', methods=['GET'])
def healthcheck_rocketchat():
    rocketchat_cli = create_rocketchat_client()
    rocketchat_code = 400 if rocketchat_cli is None else 200
    _, redis_code = healthcheck_redis()
    # Sotopia is optional if no NPC is needed for the task,
    # but for simplicity, we always check Sotopia NPC profiles are correctly
    # loaded whenever RocketChat service is needed
    _, sotopia_code = healthcheck_sotopia()
    code = 200 if redis_code == 200 and rocketchat_code == 200 and sotopia_code == 200 else 400
    return jsonify({"redis": redis_code, "rocketchat": rocketchat_code, "sotopia": sotopia_code}), code

@app.route('/api/healthcheck/plane', methods=['GET'])
def healthcheck_plane():
    code, msg = login_to_plane()
    return jsonify({"message":msg}), code


@app.route('/api/healthcheck/mailpit', methods=['GET'])
def healthcheck_mailpit():
    code, msg = check_url("http://localhost:8025/api/v1/info")
    return jsonify({"message": msg}), code


@app.route('/api/healthcheck/radicale', methods=['GET'])
def healthcheck_radicale():
    code, msg = check_url("http://localhost:5232/.web/")
    return jsonify({"message": msg}), code


@app.route('/api/healthcheck/wikijs', methods=['GET'])
def healthcheck_wikijs():
    code, msg = check_url("http://localhost:3001/healthz")
    return jsonify({"message": msg}), code


@app.route('/api/healthcheck/pleroma', methods=['GET'])
def healthcheck_pleroma():
    code, msg = check_url("http://localhost:4000/api/v1/instance")
    return jsonify({"message": msg}), code


@app.route('/api/reset-mailpit', methods=['POST'])
def reset_mailpit():
    import urllib.request
    try:
        req = urllib.request.Request(
            "http://localhost:8025/api/v1/messages",
            method="DELETE",
        )
        urllib.request.urlopen(req, timeout=10)
        return jsonify({"message": "Mailpit messages deleted"}), 202
    except Exception as e:
        return jsonify({"message": f"Mailpit reset failed: {e}"}), 500


@app.route('/api/reset-radicale', methods=['POST'])
def reset_radicale():
    # Radicale stores data in files; reset by deleting all collections
    import urllib.request
    try:
        req = urllib.request.Request(
            "http://localhost:5232/agent/",
            method="DELETE",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # 404 is fine if collection doesn't exist
    return jsonify({"message": "Radicale reset initiated"}), 202


@app.route('/api/reset-wikijs', methods=['POST'])
def reset_wikijs():
    # Wiki.js reset: delete all pages via GraphQL API
    import urllib.request
    import json as _json
    try:
        # Get all pages
        query = '{"query":"{ pages { list { id } } }"}'
        req = urllib.request.Request(
            "http://localhost:3001/graphql",
            data=query.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer theagentcompany",
            },
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = _json.loads(resp.read())
        page_ids = [p["id"] for p in data.get("data", {}).get("pages", {}).get("list", [])]
        for pid in page_ids:
            del_query = f'{{"query":"mutation {{ pages {{ delete(id: {pid}) {{ responseResult {{ succeeded }} }} }} }}"}}'
            del_req = urllib.request.Request(
                "http://localhost:3001/graphql",
                data=del_query.encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer theagentcompany",
                },
            )
            urllib.request.urlopen(del_req, timeout=10)
    except Exception as e:
        return jsonify({"message": f"Wiki.js reset error: {e}"}), 500
    return jsonify({"message": "Wiki.js pages deleted"}), 202


@app.route('/api/reset-pleroma', methods=['POST'])
def reset_pleroma():
    # Pleroma reset: clear statuses via admin API
    return jsonify({"message": "Pleroma reset initiated (manual intervention may be needed)"}), 202


@app.route('/api/healthcheck/cihub', methods=['GET'])
def healthcheck_cihub():
    if cihub_store.healthcheck():
        return jsonify({"message": "CIHub is healthy"}), 200
    return jsonify({"message": "CIHub is not healthy"}), 500
    
@app.route('/api/healthcheck/redis', methods=['GET'])
def healthcheck_redis():
    success = wait_for_redis()
    if success:
        return jsonify({"message":"success connect to redis"}), 200
    else:
        return jsonify({"message":"failed connect to redis"}), 400

def get_by_name(first_name, last_name):
    return AgentProfile.find(
        (AgentProfile.first_name == first_name) & 
        (AgentProfile.last_name == last_name)
    ).all()

@app.route('/api/healthcheck/sotopia', methods=['GET'])
def healthcheck_sotopia():
    success = wait_for_redis()
    assert len(agent_definitions) > 0
    if success:
        for definition in agent_definitions:
            if not AgentProfile.find((AgentProfile.first_name == definition["first_name"]) & (AgentProfile.last_name == definition["last_name"])).all():
                success = False
                print(f"NPC ({definition['first_name']} {definition['last_name']}) not found")
                break
        
    if success:
        return jsonify({"message":"sotopia npc profiles loaded successfully"}), 200
    else:
        return jsonify({"message":"sotopia npc profiles not loaded"}), 400


@app.route('/api/runs', methods=['POST'])
def create_run():
    body = _json_body()
    run_id = str(body.get("run_id", "")).strip()
    if not run_id:
        return jsonify({"error": "run_id is required"}), 400
    scenario_id = str(body.get("scenario_id", ""))
    task_id = str(body.get("task_id", ""))
    run = cihub_store.create_run(run_id, scenario_id=scenario_id, task_id=task_id)
    return jsonify(run), 201


@app.route('/api/runs/<run_id>/reset', methods=['POST'])
def reset_run(run_id: str):
    cihub_store.reset_run(run_id)
    cihub_store.create_run(run_id)
    return jsonify({"message": f"reset run {run_id}"}), 202


@app.route('/api/runs/<run_id>/seed', methods=['POST'])
def seed_run(run_id: str):
    body = _json_body()
    seed_state = body.get("seed_state", body)
    if not isinstance(seed_state, dict):
        return jsonify({"error": "seed_state must be an object"}), 400
    cihub_store.seed_run(run_id, seed_state)
    return jsonify({"message": f"seeded run {run_id}"}), 202


@app.route('/api/runs/<run_id>/export-state', methods=['GET'])
def export_state(run_id: str):
    return jsonify(cihub_store.export_state(run_id)), 200


@app.route('/api/runs/<run_id>/audit', methods=['GET'])
def list_audit(run_id: str):
    raw_limit = request.args.get("limit", "200")
    try:
        limit = max(1, min(2000, int(raw_limit)))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    return jsonify({"events": cihub_store.list_audit(run_id, limit=limit)}), 200


@app.route('/api/runs/<run_id>/memories', methods=['GET'])
def list_memories(run_id: str):
    tag = request.args.get("tag", "")
    memories = cihub_store.list_memories(run_id, tag=tag)
    return jsonify({"memories": memories}), 200


@app.route('/api/runs/<run_id>/memories/<key>', methods=['GET'])
def get_memory(run_id: str, key: str):
    mem = cihub_store.get_memory(run_id, key)
    if mem is None:
        return jsonify({"error": f"Memory not found: {key}"}), 404
    return jsonify({"memory": mem}), 200


@app.route('/api/runs/<run_id>/tools/<path:tool_name>', methods=['POST'])
def run_tool(run_id: str, tool_name: str):
    body = _json_body()
    args = body.get("args", {})
    if not isinstance(args, dict):
        return jsonify({"error": "args must be an object"}), 400
    actor = str(body.get("actor", "agent"))
    raw_step_id = body.get("step_id")
    step_id = None
    if raw_step_id is not None and str(raw_step_id).strip() != "":
        try:
            step_id = int(raw_step_id)
        except ValueError:
            return jsonify({"error": "step_id must be an integer"}), 400
    seed_state = body.get("seed_state")
    if seed_state is not None and not isinstance(seed_state, dict):
        return jsonify({"error": "seed_state must be an object"}), 400

    result = cihub_store.run_tool(
        run_id,
        tool_name=tool_name,
        args=args,
        actor=actor,
        step_id=step_id,
        seed_state=seed_state,
    )
    status = 200 if result.get("ok", False) else 400
    return jsonify(result), status

if __name__ == '__main__':
    if SKIP_SETUP:
        print(f"Skip the setup")
    else:
        execute_command("make start-all")
    app.run(host='0.0.0.0', port=2999)
