"""
JARVIS Memory Graph & Multi-Session Persistent Context

Implements:
1. SQLite Multi-Session Storage: Persists all conversation sessions, messages, and actions across restarts.
2. NetworkX & Graphify Knowledge Graph: Connects user preferences, entities, tasks, code topics, and tool runs.
3. Graph-RAG Retrieval: Traverses neighborhood subgraphs to recall past context, code discussions, and facts.
4. Auto-summarization and session indexing.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import networkx as nx
except ImportError:
    nx = None

log = logging.getLogger("fixo.memory_graph")

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "fixo.db"
LEGACY_DB_PATH = DATA_DIR / "jarvis.db"

if not DB_PATH.exists() and LEGACY_DB_PATH.exists():
    try:
        import shutil
        shutil.copy2(LEGACY_DB_PATH, DB_PATH)
    except Exception:
        DB_PATH = LEGACY_DB_PATH

GRAPH_JSON_PATH = DATA_DIR / "knowledge_graph.json"
GRAPHIFY_DIR = Path(__file__).parent / "graphify-out"

_active_session_id: Optional[str] = None
_in_memory_graph: Any = None


def _get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_graph_db():
    """Create session and graph tables if they don't exist."""
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            summary TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            action_json TEXT DEFAULT '',
            model TEXT DEFAULT '',
            latency_ms REAL DEFAULT 0,
            timestamp TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_created ON chat_messages(created_at);

        CREATE TABLE IF NOT EXISTS graph_entities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            metadata_json TEXT DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS graph_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            created_at REAL NOT NULL,
            FOREIGN KEY (source_id) REFERENCES graph_entities(id),
            FOREIGN KEY (target_id) REFERENCES graph_entities(id)
        );

        CREATE INDEX IF NOT EXISTS idx_relations_source ON graph_relations(source_id);
        CREATE INDEX IF NOT EXISTS idx_relations_target ON graph_relations(target_id);
    """)
    conn.close()
    _init_networkx_graph()
    log.info("Memory graph database and tables initialized.")


def _init_networkx_graph():
    """Load or initialize in-memory NetworkX directed graph."""
    global _in_memory_graph
    if nx is None:
        return

    _in_memory_graph = nx.DiGraph()
    if GRAPH_JSON_PATH.exists():
        try:
            with open(GRAPH_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                _in_memory_graph = nx.node_link_graph(data, directed=True)
                log.info(f"Loaded existing knowledge graph: {_in_memory_graph.number_of_nodes()} nodes, {_in_memory_graph.number_of_edges()} edges")
                return
        except Exception as e:
            log.warning(f"Could not load knowledge_graph.json: {e}")

    # Build base nodes
    _in_memory_graph.add_node("User", type="Entity", name="User", label="User")
    _in_memory_graph.add_node("JARVIS", type="System", name="JARVIS", label="JARVIS Assistant")
    _in_memory_graph.add_node("Himmatnagar", type="Location", name="Himmatnagar, Gujarat", label="User Location")
    _in_memory_graph.add_edge("User", "Himmatnagar", relation="LOCATED_IN")
    _in_memory_graph.add_edge("JARVIS", "User", relation="ASSISTS")
    _save_networkx_graph()


def _save_networkx_graph():
    """Save in-memory graph to JSON."""
    if _in_memory_graph is None or nx is None:
        return
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = nx.node_link_data(_in_memory_graph)
        with open(GRAPH_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.warning(f"Failed to save knowledge graph JSON: {e}")


# ---------------------------------------------------------------------------
# Multi-Session Management
# ---------------------------------------------------------------------------

def create_session(title: str = "New Conversation") -> str:
    """Create a new chat session and make it active."""
    global _active_session_id
    session_id = f"session_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    now = time.time()
    conn = _get_db()
    conn.execute(
        "INSERT INTO chat_sessions (id, title, created_at, updated_at, summary) VALUES (?, ?, ?, ?, ?)",
        (session_id, title, now, now, "")
    )
    conn.commit()
    conn.close()

    _active_session_id = session_id

    # Add to graph
    if _in_memory_graph is not None:
        _in_memory_graph.add_node(session_id, type="Session", title=title, created_at=now)
        _in_memory_graph.add_edge("User", session_id, relation="STARTED_SESSION")
        _save_networkx_graph()

    log.info(f"Created new chat session: {session_id} ({title})")
    return session_id


def get_or_create_active_session() -> str:
    """Get currently active session or load latest from DB or create a new one."""
    global _active_session_id
    if _active_session_id:
        return _active_session_id

    conn = _get_db()
    cur = conn.execute("SELECT id FROM chat_sessions ORDER BY updated_at DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()

    if row:
        _active_session_id = row["id"]
        return _active_session_id

    return create_session("Initial Conversation")


def set_active_session(session_id: str) -> bool:
    """Set the currently active session."""
    global _active_session_id
    conn = _get_db()
    cur = conn.execute("SELECT id FROM chat_sessions WHERE id = ?", (session_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        _active_session_id = session_id
        return True
    return False


def list_sessions(limit: int = 50) -> List[Dict[str, Any]]:
    """List conversation sessions with message count and preview."""
    conn = _get_db()
    cur = conn.execute("""
        SELECT s.id, s.title, s.created_at, s.updated_at, s.summary,
               COUNT(m.id) as message_count,
               (SELECT text FROM chat_messages WHERE session_id = s.id ORDER BY created_at DESC LIMIT 1) as last_message
        FROM chat_sessions s
        LEFT JOIN chat_messages m ON s.id = m.session_id
        GROUP BY s.id
        ORDER BY s.updated_at DESC
        LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()

    sessions = []
    for r in rows:
        sessions.append({
            "id": r["id"],
            "title": r["title"] or "Conversation",
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "formatted_date": datetime.fromtimestamp(r["updated_at"]).strftime("%b %d, %I:%M %p"),
            "summary": r["summary"] or "",
            "message_count": r["message_count"] or 0,
            "last_message": r["last_message"] or "",
            "is_active": (r["id"] == _active_session_id),
        })
    return sessions


def get_session_messages(session_id: Optional[str] = None, limit: int = 150) -> List[Dict[str, Any]]:
    """Retrieve full formatted conversation log for a specific session."""
    sid = session_id or get_or_create_active_session()
    conn = _get_db()
    cur = conn.execute("""
        SELECT id, role, text, action_json, model, latency_ms, timestamp, created_at
        FROM chat_messages
        WHERE session_id = ?
        ORDER BY created_at ASC
        LIMIT ?
    """, (sid, limit))
    rows = cur.fetchall()
    conn.close()

    messages = []
    for r in rows:
        action = None
        if r["action_json"]:
            try:
                action = json.loads(r["action_json"])
            except Exception:
                pass
        messages.append({
            "id": r["id"],
            "role": r["role"],
            "text": r["text"],
            "action": action,
            "model": r["model"],
            "latency_ms": r["latency_ms"],
            "timestamp": r["timestamp"],
            "created_at": r["created_at"],
        })
    return messages


def save_chat_message(
    role: str,
    text: str,
    action: Optional[Dict[str, Any]] = None,
    model: str = "",
    latency_ms: float = 0.0,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a chat message to SQLite and update knowledge graph."""
    sid = session_id or get_or_create_active_session()
    msg_id = f"msg_{int(time.time() * 1000)}_{uuid.uuid4().hex[:4]}"
    now = time.time()
    ts_str = datetime.now().strftime("%H:%M:%S")
    action_json = json.dumps(action) if action else ""

    conn = _get_db()
    conn.execute("""
        INSERT INTO chat_messages (id, session_id, role, text, action_json, model, latency_ms, timestamp, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (msg_id, sid, role, text, action_json, model, latency_ms, ts_str, now))

    # Update session title if first user message
    cur = conn.execute("SELECT COUNT(*) as cnt FROM chat_messages WHERE session_id = ?", (sid,))
    count = cur.fetchone()["cnt"]
    if count <= 2 and role == "user":
        clean_title = text.strip()[:40]
        if len(text.strip()) > 40:
            clean_title += "..."
        conn.execute("UPDATE chat_sessions SET title = ?, updated_at = ? WHERE id = ?", (clean_title, now, sid))
    else:
        conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, sid))

    conn.commit()
    conn.close()

    # Ingest into Graph
    _ingest_message_to_graph(sid, msg_id, role, text, action)

    return {
        "id": msg_id,
        "session_id": sid,
        "role": role,
        "text": text,
        "action": action,
        "model": model,
        "latency_ms": latency_ms,
        "timestamp": ts_str,
        "created_at": now,
    }


def _ingest_message_to_graph(session_id: str, msg_id: str, role: str, text: str, action: Optional[Dict[str, Any]]):
    """Link message, extracted topics, actions, and entities in NetworkX."""
    if _in_memory_graph is None or nx is None:
        return

    try:
        # Add message node
        _in_memory_graph.add_node(
            msg_id,
            type="Message",
            role=role,
            preview=text[:60],
            length=len(text),
        )
        _in_memory_graph.add_edge(session_id, msg_id, relation="CONTAINS_MESSAGE")

        # Action links
        if action:
            act_type = action.get("action", "unknown")
            act_target = action.get("target", "")
            act_node_id = f"action_{act_type}_{act_target[:20]}"
            _in_memory_graph.add_node(act_node_id, type="Action", action=act_type, target=act_target)
            _in_memory_graph.add_edge(msg_id, act_node_id, relation="TRIGGERED_ACTION")

        # Extract keywords / topics
        lower = text.lower()
        key_topics = {
            "flight": "Flights & Travel",
            "dubai": "Dubai Trip",
            "ahmedabad": "Ahmedabad Travel",
            "goa": "Goa Trip",
            "himmatnagar": "Himmatnagar",
            "katana": "Katana & Demon Slayer",
            "website": "Web Development",
            "code": "Software Engineering",
            "opencode": "OpenCode Engine",
            "spotify": "Spotify Music",
            "research": "Deep Research",
            "mca": "MCA Education",
            "college": "College Search",
        }
        for kw, topic_name in key_topics.items():
            if kw in lower:
                topic_id = f"topic_{kw}"
                if not _in_memory_graph.has_node(topic_id):
                    _in_memory_graph.add_node(topic_id, type="Topic", name=topic_name)
                _in_memory_graph.add_edge(msg_id, topic_id, relation="DISCUSSED_TOPIC")
                _in_memory_graph.add_edge("User", topic_id, relation="INTERESTED_IN")

        _save_networkx_graph()
    except Exception as e:
        log.debug(f"Error ingesting message to graph: {e}")


# ---------------------------------------------------------------------------
# Graph-RAG Retrieval & Query Engine
# ---------------------------------------------------------------------------

def query_graph_context(query: str, max_results: int = 5) -> str:
    """Retrieve connected knowledge graph context relevant to a query."""
    if _in_memory_graph is None or nx is None:
        return ""

    tokens = [t.strip().lower() for t in query.split() if len(t.strip()) > 3]
    matched_nodes = []

    for node, attrs in _in_memory_graph.nodes(data=True):
        name = str(attrs.get("name", "")).lower()
        title = str(attrs.get("title", "")).lower()
        preview = str(attrs.get("preview", "")).lower()
        target = str(attrs.get("target", "")).lower()
        node_str = f"{node} {name} {title} {preview} {target}"

        score = sum(1 for tok in tokens if tok in node_str)
        if score > 0:
            matched_nodes.append((score, node, attrs))

    matched_nodes.sort(key=lambda x: x[0], reverse=True)
    if not matched_nodes:
        return ""

    context_lines = ["\n[KNOWLEDGE GRAPH MEMORY CONTEXT]"]
    seen_edges = set()

    for _, node, attrs in matched_nodes[:max_results]:
        node_type = attrs.get("type", "Node")
        label = attrs.get("name") or attrs.get("title") or attrs.get("preview") or str(node)
        context_lines.append(f"• {node_type}: {label}")

        # Add 1-hop neighbors
        for neighbor in _in_memory_graph.neighbors(node):
            edge_data = _in_memory_graph.get_edge_data(node, neighbor) or {}
            rel = edge_data.get("relation", "CONNECTED_TO")
            n_attrs = _in_memory_graph.nodes[neighbor]
            n_label = n_attrs.get("name") or n_attrs.get("title") or n_attrs.get("preview") or str(neighbor)
            edge_key = (str(node), str(neighbor))
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                context_lines.append(f"  └─ [{rel}] → {n_label}")

    return "\n".join(context_lines)


def get_graph_stats() -> Dict[str, Any]:
    """Get statistics about the knowledge graph and sessions."""
    node_count = _in_memory_graph.number_of_nodes() if _in_memory_graph is not None else 0
    edge_count = _in_memory_graph.number_of_edges() if _in_memory_graph is not None else 0

    conn = _get_db()
    cur = conn.execute("SELECT COUNT(*) as s_count FROM chat_sessions")
    s_count = cur.fetchone()["s_count"]

    cur = conn.execute("SELECT COUNT(*) as m_count FROM chat_messages")
    m_count = cur.fetchone()["m_count"]
    conn.close()

    return {
        "nodes": node_count,
        "edges": edge_count,
        "sessions": s_count,
        "messages": m_count,
        "active_session": _active_session_id,
    }
