# Copyright (C) 2025-2026 [Vasile Sabo / Remotex]
#
# This file is part of LLuna.
#
# LLuna is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# LLuna is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with LLuna. If not, see <https://www.gnu.org/licenses/>.

#!/usr/bin/env python3
"""
LLuna v6.0 - Autonomous AI Agent
=================================
Tool Execution Integrity + Anti-Hallucination + Loop Discipline

HARD RULES:
- Tools = ground truth
- No pretending execution
- No inferred reality
- Prefer refusal over hallucination
- Constrain small models aggressively

Run: python app.py
"""

import asyncio
import logging
import os
import sys
import yaml
import time
import threading
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit
from functools import wraps

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24).hex()

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='threading',
    ping_timeout=60,
    ping_interval=25
)

config = {}
llm_manager = None
mcp_client = None
agent = None
startup_complete = False
stream_enabled = True  # Stream output toggle


def load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    return {}


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def safe_json(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return wrapper


def initialize():
    global config, llm_manager, mcp_client, agent, startup_complete
    
    logger.info("=" * 60)
    logger.info("  LLuna v6.0 - Execution Integrity + Anti-Hallucination")
    logger.info("=" * 60)
    
    config = load_config()
    
    from llm import LLMManager
    llm_manager = LLMManager(config)
    
    from mcp_client import MCPClient
    mcp_client = MCPClient()
    mcp_client.load_servers_from_config(config)
    mcp_client.auto_discover_servers()
    
    def on_progress(server, status, current, total):
        socketio.emit('mount_progress', {"server": server, "status": status, "current": current, "total": total})
    
    mcp_client.on_mount_progress(on_progress)
    
    from agent import AutonomousAgent
    ac = config.get("agent", {})
    agent = AutonomousAgent(
        llm_manager=llm_manager,
        mcp_client=mcp_client,
        max_iterations=ac.get("max_iterations", 20),
        auto_approve_safe=ac.get("auto_execute", False),
        max_context_tokens=ac.get("max_context_tokens", 4096)
    )
    
    if config.get("startup", {}).get("auto_mount", True):
        results = run_async(mcp_client.start_all_servers(parallel=True))
        logger.info(f"Mounted {sum(1 for v in results.values() if v)}/{len(results)} servers")
    
    startup_complete = True
    logger.info("Ready!")


# Routes
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
@safe_json
def get_status():
    return jsonify({
        "startup_complete": startup_complete,
        "llm": llm_manager.get_status() if llm_manager else None,
        "mcp": mcp_client.get_stats() if mcp_client else None,
        "agent": agent.get_stats() if agent else None
    })


@app.route('/api/llm/providers')
@safe_json
def get_providers():
    return jsonify(llm_manager.get_provider_info())


@app.route('/api/servers')
@safe_json
def get_servers():
    return jsonify(mcp_client.get_server_status())


@app.route('/api/servers/<n>/start', methods=['POST'])
@safe_json
def start_server(name):
    return jsonify({"success": run_async(mcp_client.start_server(name))})


@app.route('/api/servers/<n>/stop', methods=['POST'])
@safe_json
def stop_server(name):
    return jsonify({"success": run_async(mcp_client.stop_server(name))})


@app.route('/api/tools')
@safe_json
def get_tools():
    return jsonify([{"name": t.name, "description": t.description, "server": t.server_name} for t in mcp_client.get_all_tools()])


@app.route('/api/agent/stats')
@safe_json
def get_agent_stats():
    return jsonify(agent.get_stats())


@app.route('/api/chat/clear', methods=['POST'])
@safe_json
def clear_chat():
    agent.clear_context()
    return jsonify({"success": True})


# WebSocket
@socketio.on('connect')
def handle_connect():
    emit('status', {
        'startup_complete': startup_complete,
        'llm': llm_manager.get_status() if llm_manager else None,
        'servers': mcp_client.get_server_status() if mcp_client else {},
        'stats': agent.get_stats() if agent else {}
    })


@socketio.on('connect_llm')
def handle_connect_llm(data):
    success = llm_manager.connect(data.get('provider', 'ollama'), data.get('model'))
    emit('llm_status', llm_manager.get_status())


@socketio.on('disconnect_llm')
def handle_disconnect_llm():
    llm_manager.disconnect()
    emit('llm_status', llm_manager.get_status())


@socketio.on('start_servers')
def handle_start_servers():
    def run():
        results = run_async(mcp_client.start_all_servers())
        socketio.emit('servers_status', {'status': mcp_client.get_server_status()})
        socketio.emit('mount_complete', {'results': results})
    threading.Thread(target=run, daemon=True).start()


@socketio.on('stop_all_servers')
def handle_stop_all():
    run_async(mcp_client.stop_all_servers())
    emit('servers_status', {'status': mcp_client.get_server_status()})


@socketio.on('set_auto_execute')
def handle_auto_execute(data):
    agent.auto_approve_safe = data.get('enabled', False)


@socketio.on('set_stream')
def handle_set_stream(data):
    # Store stream preference (used when calling LLM)
    global stream_enabled
    stream_enabled = data.get('enabled', True)
    logger.info(f"Stream output: {stream_enabled}")


@socketio.on('chat')
def handle_chat(data):
    msg = data.get('message', '').strip()
    if not msg:
        emit('error', {'message': 'Empty'})
        return
    
    def on_event(ev):
        socketio.emit('cognitive_event', {
            'state': ev.state.value,
            'thought': ev.thought,
            'tool_call_id': ev.tool_call_id,
            'tool_name': ev.tool_name,
            'tool_server': ev.tool_server,
            'tool_args': ev.tool_args,
            'tool_result': ev.tool_result,
            'tool_error': ev.tool_error,
            'tool_state': ev.tool_state,
            'duration_ms': ev.duration_ms,
            'requires_approval': ev.requires_approval,
            'iteration': ev.iteration,
            'prompt_id': ev.prompt_id,
            'tokens_used': ev.tokens_used,
            'confidence': ev.confidence,
            'reasoning_unit': ev.reasoning_unit,
            'violation_type': ev.violation_type,
        })
    
    def run():
        try:
            response = run_async(agent.process(msg, on_event=on_event))
            socketio.emit('response', {
                'content': response,
                'stats': agent.get_stats() if agent else {}
            })
        except Exception as e:
            socketio.emit('error', {'message': str(e)})
    
    threading.Thread(target=run, daemon=True).start()


@socketio.on('stop_agent')
def handle_stop():
    if agent:
        agent.stop()
        emit('agent_stopped', {})


@socketio.on('approve_tool')
def handle_approve():
    logger.info("Approval received from UI")
    if agent:
        result = agent.approve_pending()
        logger.info(f"Approval result: {result}")


@socketio.on('reject_tool')
def handle_reject():
    logger.info("Rejection received from UI")
    if agent:
        agent.reject_pending()


@socketio.on('sudo_approve')
def handle_sudo_approve(data):
    logger.info("Sudo approval received")
    password = data.get('password', '')
    if agent:
        result = agent.approve_sudo(password)
        emit('sudo_result', {'success': result})


@socketio.on('sudo_reject')
def handle_sudo_reject():
    logger.info("Sudo rejection received")
    if agent:
        agent.reject_sudo()
        emit('sudo_result', {'success': False, 'error': 'User denied sudo'})


def main():
    initialize()
    host = os.environ.get('LLUNA_HOST', '0.0.0.0')
    port = int(os.environ.get('LLUNA_PORT', 5000))
    logger.info(f"http://{host}:{port}")
    socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True, use_reloader=False)


if __name__ == '__main__':
    main()
