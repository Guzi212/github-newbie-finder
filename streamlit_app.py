"""Streamlit UI — 5 pages: Search · Recommendations · Tutorial · Favorites · History.

Talks to the FastAPI backend over HTTP at API_BASE_URL. Keeps no business
logic; this is a thin shell that renders Pydantic schemas.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st
from dotenv import load_dotenv

from app.exporters import (
    recommendations_filename,
    render_recommendations_md,
    render_tutorial_md,
    tutorial_filename,
)

load_dotenv()

API_BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")

st.set_page_config(
    page_title="GitHub 小白检索器",
    page_icon="🧭",
    layout="wide",
)

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


# Split timeouts: connect quickly, allow long reads for the rank endpoint
# (which fans out to several LLM calls + GitHub fetches).
_TIMEOUTS = {
    "/api/requirements/parse": 90.0,
    "/api/search": 120.0,
    "/api/recommendations/rank": 240.0,
    "/api/tutorials/generate": 180.0,
}
_DEFAULT_TIMEOUT = 60.0


def _timeout_for(path: str) -> httpx.Timeout:
    read = _TIMEOUTS.get(path, _DEFAULT_TIMEOUT)
    return httpx.Timeout(connect=10.0, read=read, write=15.0, pool=read)


def _post(path: str, payload: dict | None = None) -> dict:
    try:
        r = httpx.post(f"{API_BASE}{path}", json=payload or {}, timeout=_timeout_for(path))
    except httpx.ReadTimeout:
        raise RuntimeError(
            f"{path} 读取超时。LLM 接口可能临时不稳；可在「API Key 管理」里换一个 key，"
            "或把 `.env` 里的 `LLM_PROVIDER` 改成 `echo` 临时降级走演示模式。"
        )
    except httpx.ConnectError:
        raise RuntimeError(f"无法连接后端 {API_BASE}。请确认 `uvicorn app.main:app` 已启动。")
    if r.status_code >= 400:
        raise RuntimeError(f"{path} → {r.status_code}: {r.text[:400]}")
    return r.json()


def _get(path: str) -> Any:
    try:
        r = httpx.get(f"{API_BASE}{path}", timeout=httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=30.0))
    except httpx.ReadTimeout:
        raise RuntimeError(f"{path} 读取超时。")
    except httpx.ConnectError:
        raise RuntimeError(f"无法连接后端 {API_BASE}。")
    if r.status_code >= 400:
        raise RuntimeError(f"{path} → {r.status_code}: {r.text[:400]}")
    return r.json()


def _delete(path: str) -> dict:
    try:
        r = httpx.delete(f"{API_BASE}{path}", timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=15.0))
    except httpx.ConnectError:
        raise RuntimeError(f"无法连接后端 {API_BASE}。")
    if r.status_code >= 400:
        raise RuntimeError(f"{path} → {r.status_code}: {r.text[:400]}")
    return r.json() if r.text else {"ok": True}


def _api_alive() -> bool:
    import time

    for attempt in range(2):
        try:
            r = httpx.get(
                f"{API_BASE}/healthz",
                timeout=httpx.Timeout(connect=2.0, read=5.0, write=2.0, pool=5.0),
            )
            if r.status_code < 400:
                return True
        except Exception:
            pass
        if attempt == 0:
            time.sleep(0.4)
    return False


def _row_columns(spec):
    """st.columns with vertical centring on streamlit >= 1.36.

    Falls back gracefully on older versions so the layout still works,
    just without perfect centring.
    """
    try:
        return st.columns(spec, vertical_alignment="center")
    except TypeError:
        return st.columns(spec)


# Apply any pending navigation request from a button click. We have to do this
# *before* the sidebar radio is created, because once a widget is bound to a
# session_state key, you can't reassign that key from regular code.
if "_pending_nav" in st.session_state:
    st.session_state["nav"] = st.session_state.pop("_pending_nav")


# ---------------------------------------------------------------------------
# Sidebar — profile + API key management
# ---------------------------------------------------------------------------


def _sidebar_profile() -> dict:
    st.sidebar.header("👤 我的画像")
    os_choice = st.sidebar.selectbox("操作系统", ["macos", "windows", "linux", "unknown"], index=0)
    skill = st.sidebar.selectbox(
        "水平", ["beginner", "intermediate", "advanced"], index=0,
        help="选「beginner」就行，教程会更详细。",
    )
    preferred_language = st.sidebar.selectbox("教程语言", ["zh", "en"], index=0)
    return {
        "os": os_choice,
        "skill_level": skill,
        # Schema-required but no longer surfaced to the user — defaults are fine.
        "has_docker": False,
        "has_gpu": False,
        "api_key_status": "none",
        "preferred_language": preferred_language,
    }


def _credentials_panel() -> None:
    """Sidebar expander for managing API keys."""
    with st.sidebar.expander("🔑 API Key 管理", expanded=False):
        try:
            creds = _get("/api/credentials") or []
        except Exception as e:
            st.error(f"读取失败：{e}")
            creds = []

        if creds:
            active_label = next(
                (f"{c['provider']} · {c['label']}" for c in creds if c.get("is_active")),
                None,
            )
            if active_label:
                st.caption(f"当前激活：**{active_label}**")
            else:
                st.caption("当前没有激活的 Key（点 ✓ 激活一条）。")

            for c in creds:
                with st.container(border=True):
                    marker = "🟢" if c.get("is_active") else "⚪"
                    st.markdown(f"{marker} **{c['provider']}** · {c['label']}")
                    st.caption(
                        f"{c['key_preview']} · model `{c.get('model') or '默认'}`"
                    )
                    if c.get("is_active"):
                        if st.button("🗑 删除", key=f"del_cred_{c['id']}",
                                     use_container_width=True):
                            try:
                                _delete(f"/api/credentials/{c['id']}")
                                st.toast("已删除")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))
                    else:
                        bcols = st.columns(2)
                        if bcols[0].button("✓ 激活", key=f"act_cred_{c['id']}",
                                           use_container_width=True):
                            try:
                                _post(f"/api/credentials/{c['id']}/activate")
                                st.toast("已切换")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))
                        if bcols[1].button("🗑 删除", key=f"del_cred_{c['id']}",
                                           use_container_width=True):
                            try:
                                _delete(f"/api/credentials/{c['id']}")
                                st.toast("已删除")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))
        else:
            st.caption("还没有保存的 Key —— 当前会回退到 .env 中的配置。")

        st.divider()
        st.markdown("**+ 添加 / 更新 Key**")
        with st.form(key="add_cred_form", clear_on_submit=True):
            provider = st.selectbox("provider", ["deepseek", "openai", "anthropic"])
            label = st.text_input("备注名（同 provider 同名会覆盖）", value="默认")
            api_key = st.text_input("API Key", type="password")
            model = st.text_input("model（可留空走默认）", value="")
            base_url = st.text_input("base_url（可留空，仅 deepseek/自托管 需要）", value="")
            submitted = st.form_submit_button("保存", use_container_width=True)
        if submitted:
            if not api_key.strip():
                st.error("API Key 不能为空")
            else:
                try:
                    _post("/api/credentials", {
                        "provider": provider,
                        "label": label.strip() or provider,
                        "api_key": api_key.strip(),
                        "model": model.strip(),
                        "base_url": base_url.strip(),
                    })
                    st.toast("已保存")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))


PROFILE = _sidebar_profile()
_credentials_panel()
st.sidebar.divider()
if not _api_alive():
    st.sidebar.error(
        f"API 未连通：{API_BASE}\n"
        "请先启动后端：`bash scripts/run-backend.sh`"
    )
    if st.sidebar.button("🔄 重新检测", use_container_width=True):
        st.rerun()
else:
    st.sidebar.success(f"API: {API_BASE}")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def page_search() -> None:
    st.title("🧭 GitHub 小白检索器")
    st.caption("用一句话描述你想做的事，工具帮你找项目、判断难度，并写一份新手部署教程。")

    raw_text = st.text_area(
        "你的需求",
        placeholder="例：想找一个适合新手本地部署的 RAG 知识库项目，最好有 Web UI，不想折腾 Docker。",
        height=120,
        key="raw_text",
    )

    cols = st.columns([1, 1, 4])
    with cols[0]:
        run = st.button("🔍 解析 + 检索 + 排序", type="primary", use_container_width=True)
    with cols[1]:
        parse_only = st.button("仅解析需求", use_container_width=True)

    if parse_only and raw_text.strip():
        try:
            with st.spinner("解析需求…"):
                data = _post("/api/requirements/parse",
                             {"raw_text": raw_text, "user_profile": PROFILE})
        except RuntimeError as e:
            st.error(str(e))
            return
        st.session_state["parsed"] = data
        st.success(f"已解析，query_id = {data['query_id']}")
        st.json(data["parsed"])
        st.subheader("生成的检索查询")
        for q in data["queries"]:
            st.markdown(f"- `{q['q']}`  · _{q['why']}_")

    if run and raw_text.strip():
        try:
            with st.spinner("解析需求…"):
                parsed = _post("/api/requirements/parse",
                               {"raw_text": raw_text, "user_profile": PROFILE})
            st.session_state["parsed"] = parsed
            with st.spinner("调用 GitHub 召回候选仓库…"):
                search = _post("/api/search", {"query_id": parsed["query_id"]})
            st.session_state["search"] = search

            top_n = _adaptive_top_n(search.get("candidates") or [])
            st.info(
                f"召回 {len(search['candidates'])} 个候选，"
                f"高 star (≥500) 候选 {sum(1 for c in search['candidates'] if c.get('stars', 0) >= 500)} 个，"
                f"将推荐 Top-{top_n}。"
            )
            with st.spinner("抓取快照 + 规则评分 + LLM 重排…"):
                ranked = _post("/api/recommendations/rank",
                               {"query_id": parsed["query_id"], "top_n": top_n,
                                "user_profile": PROFILE})
        except RuntimeError as e:
            st.error(str(e))
            return
        st.session_state["ranked"] = ranked
        st.session_state["query_id"] = parsed["query_id"]
        st.session_state["raw_text_for_export"] = raw_text
        st.session_state["query_tutorials"] = []  # fresh query, no tutorials yet
        st.session_state["_pending_nav"] = "Recommendations"
        st.rerun()


def _adaptive_top_n(candidates: list[dict]) -> int:
    """Decide how many recommendations to surface.

    When there are many high-quality candidates (stars >= 500), give the user
    a wider shortlist; otherwise keep the default 5 to avoid noise.
    """
    high_star = sum(1 for c in candidates if c.get("stars", 0) >= 500)
    if high_star >= 8:
        return min(10, high_star)
    if high_star >= 5:
        return 7
    return 5


def _render_recommendation(rec: dict, idx: int, query_id: int | None) -> None:
    repo = rec["repo"]
    rs = rec["rule_score"]
    rerank = rec.get("rerank")
    summary_line = (rerank.get("summary") if rerank else "") or repo.get("description") or ""
    full_name = repo["full_name"]

    favorited_set: set[str] = st.session_state.setdefault("favorites_set", set())
    is_fav = full_name in favorited_set

    with st.container(border=True):
        head_l, head_r = st.columns([5, 1])
        with head_l:
            st.markdown(f"### #{rec['final_rank']} · [{full_name}]({repo['url']})")
            if summary_line:
                st.markdown(f"**📌 项目用途**：{summary_line}")
            if rerank and repo.get("description") and repo["description"] != summary_line:
                st.caption(f"GitHub 描述：{repo['description']}")
        with head_r:
            st.metric("综合分", f"{rec['final_score']:.1f}")
            st.caption(f"难度 {rs['deployment_level']} · ★{repo['stars']}")

        meta_cols = st.columns(5)
        meta_cols[0].markdown(f"**License**\n\n{repo.get('license') or '—'}")
        meta_cols[1].markdown(f"**Language**\n\n{repo.get('language') or '—'}")
        topics = ", ".join(repo.get("topics") or []) or "—"
        meta_cols[2].markdown(f"**Topics**\n\n{topics[:60]}")
        meta_cols[3].markdown(f"**Stars**\n\n{repo['stars']}")
        meta_cols[4].markdown(f"**Forks**\n\n{repo['forks']}")

        st.markdown("**Rule score breakdown**")
        bd_cols = st.columns(len(rs["breakdown"]))
        for col, b in zip(bd_cols, rs["breakdown"]):
            with col:
                pct = int(b["score"] * 100)
                col.markdown(f"**{b['name']}**  ·  权重 {int(b['weight']*100)}%")
                col.progress(min(1.0, b["score"]), text=f"{pct}/100")
                col.caption(b["evidence"])

        if rerank:
            with st.expander("🔎 LLM 重排分析", expanded=True):
                ll, rr = st.columns(2)
                with ll:
                    if rerank.get("pros"):
                        st.markdown("**优点**")
                        for p in rerank["pros"]:
                            st.markdown(f"- {p}")
                    if rerank.get("differentiators"):
                        st.markdown("**差异化**")
                        for d in rerank["differentiators"]:
                            st.markdown(f"- {d}")
                with rr:
                    if rerank.get("cons"):
                        st.markdown("**注意点**")
                        for c in rerank["cons"]:
                            st.markdown(f"- {c}")
                    if rerank.get("risk_flags"):
                        st.markdown("**风险**")
                        for f in rerank["risk_flags"]:
                            st.markdown(f"- ⚠️ {f}")

        if rec.get("evidence"):
            with st.expander("📎 Evidence (取自 README/元数据)"):
                for ev in rec["evidence"]:
                    src = f" · [来源]({ev['source_url']})" if ev.get("source_url") else ""
                    st.markdown(f"**{ev['kind']}**{src}")
                    st.code(ev["excerpt"][:600], language="markdown")

        st.markdown("**🧠 为什么推荐这个项目**")
        for line in (rec.get("rationale") or "").split("\n"):
            line = line.strip()
            if line:
                st.markdown(line)

        a, b, c, _ = st.columns([1.2, 1, 1, 3])
        if a.button("📘 生成部署教程", key=f"tut_{idx}", type="primary"):
            try:
                with st.spinner(f"为 {full_name} 生成教程…"):
                    payload = {"full_name": full_name, "user_profile": PROFILE}
                    if query_id is not None:
                        payload["query_id"] = query_id
                    data = _post("/api/tutorials/generate", payload)
                st.session_state[f"tutorial_{full_name}"] = data
                st.session_state["selected_repo"] = full_name
                st.session_state["_pending_nav"] = "Tutorial"
                st.rerun()
            except RuntimeError as e:
                st.error(str(e))

        if is_fav:
            if b.button("✅ 已收藏 · 取消", key=f"unfav_{idx}"):
                try:
                    _post("/api/feedback", {"full_name": full_name, "action": "unfavorite"})
                    favorited_set.discard(full_name)
                    st.toast("已取消收藏")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))
        else:
            if b.button("⭐ 收藏", key=f"fav_{idx}"):
                try:
                    _post("/api/feedback", {"full_name": full_name, "action": "favorite"})
                    favorited_set.add(full_name)
                    st.toast("已收藏")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        if c.button("🙅 不感兴趣", key=f"skip_{idx}"):
            try:
                _post("/api/feedback", {"full_name": full_name, "action": "not_interested"})
                st.toast("已记录")
            except Exception as e:
                st.error(str(e))


def _hydrate_favorites_set() -> None:
    """Pull the current favorites once per page render so star state is correct."""
    try:
        favs = _get("/api/favorites") or []
        st.session_state["favorites_set"] = {f["full_name"] for f in favs}
    except Exception:
        st.session_state.setdefault("favorites_set", set())


def page_recommendations() -> None:
    st.title("📊 推荐结果")
    _hydrate_favorites_set()

    # If the user clicked into a history entry, fetch and populate before render.
    pending_history = st.session_state.pop("_load_history_id", None)
    if pending_history is not None:
        try:
            with st.spinner("加载历史推荐…"):
                detail = _get(f"/api/queries/{pending_history}/detail")
            st.session_state["ranked"] = {
                "query_id": detail["query_id"],
                "parsed": detail["parsed"],
                "recommendations": detail["recommendations"],
            }
            st.session_state["query_id"] = detail["query_id"]
            st.session_state["raw_text_for_export"] = detail.get("raw_text", "")
            st.session_state["query_tutorials"] = detail.get("tutorials") or []
        except RuntimeError as e:
            st.error(str(e))
            return

    ranked = st.session_state.get("ranked")
    if not ranked:
        st.info("还没有结果。请先到「需求检索」页运行一次。")
        return
    qid = ranked.get("query_id")
    st.caption(
        f"query_id = {qid} · 推荐 {len(ranked['recommendations'])} 个项目")
    raw_text_for_export = st.session_state.get("raw_text_for_export", "")
    with st.expander("解析后的需求", expanded=False):
        st.json(ranked["parsed"])

    # Export bar
    if ranked["recommendations"]:
        md = render_recommendations_md(
            ranked,
            raw_text=raw_text_for_export,
            lang=PROFILE.get("preferred_language", "zh"),
        )
        st.download_button(
            label="⬇️ 导出本次推荐为 Markdown（Obsidian 友好）",
            data=md.encode("utf-8"),
            file_name=recommendations_filename(ranked),
            mime="text/markdown",
            use_container_width=False,
        )

    # Tutorials previously generated for this query
    prior = st.session_state.get("query_tutorials") or []
    if prior:
        with st.container(border=True):
            st.markdown("**📘 这次会话生成过的教程**")
            cols = st.columns(min(4, max(1, len(prior))))
            for i, t in enumerate(prior):
                with cols[i % len(cols)]:
                    if st.button(t["full_name"], key=f"goto_tut_{i}", use_container_width=True):
                        st.session_state["selected_repo"] = t["full_name"]
                        # ranked already includes the repo; tutorial fetch happens on Tutorial page
                        st.session_state["_pending_nav"] = "Tutorial"
                        st.rerun()

    if not ranked["recommendations"]:
        st.warning("没有命中候选。可以尝试在「需求检索」页换一种描述。")
        return

    # Comparison table
    st.subheader("Top 5 对比")
    rows = []
    for r in ranked["recommendations"]:
        rs = r["rule_score"]
        rows.append({
            "#": r["final_rank"],
            "Repo": r["repo"]["full_name"],
            "★": r["repo"]["stars"],
            "难度": rs["deployment_level"],
            "需求": f"{rs['fit']*100:.0f}",
            "新手": f"{rs['beginner']*100:.0f}",
            "部署": f"{rs['deployability']*100:.0f}",
            "活跃": f"{rs['activity']*100:.0f}",
            "文档": f"{rs['documentation']*100:.0f}",
            "综合": f"{r['final_score']:.1f}",
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    st.subheader("推荐卡片")
    for idx, r in enumerate(ranked["recommendations"]):
        _render_recommendation(r, idx, qid)


def page_tutorial() -> None:
    st.title("📘 个性化部署教程")
    full_name = st.session_state.get("selected_repo")
    if not full_name:
        ranked = st.session_state.get("ranked")
        if ranked and ranked["recommendations"]:
            options = [r["repo"]["full_name"] for r in ranked["recommendations"]]
            full_name = st.selectbox("选择一个项目", options)
            st.session_state["selected_repo"] = full_name
        else:
            st.info("先到「推荐结果」页选一个项目，或在下方手动输入。")
            full_name = st.text_input("repo full_name (例：gradio-app/gradio)")

    if not full_name:
        return

    cached = st.session_state.get(f"tutorial_{full_name}")
    a, b = st.columns([1, 4])
    if a.button("🔁 重新生成" if cached else "🛠 生成教程", type="primary"):
        try:
            with st.spinner("抓取仓库快照 + 生成教程…"):
                payload = {"full_name": full_name, "user_profile": PROFILE}
                qid = st.session_state.get("query_id")
                if qid is not None:
                    payload["query_id"] = qid
                data = _post("/api/tutorials/generate", payload)
            st.session_state[f"tutorial_{full_name}"] = data
            cached = data
        except RuntimeError as e:
            st.error(str(e))
            return

    data = cached
    if not data:
        return

    plan = data["plan"]
    md = render_tutorial_md(
        data,
        repo_url=f"https://github.com/{full_name}",
        lang=PROFILE.get("preferred_language", "zh"),
    )
    b.download_button(
        label="⬇️ 导出教程为 Markdown",
        data=md.encode("utf-8"),
        file_name=tutorial_filename(data),
        mime="text/markdown",
    )

    st.markdown(f"### {plan['repo_full_name']}")
    if plan.get("assumptions"):
        with st.expander("我假设你的环境是…", expanded=False):
            for a_ in plan["assumptions"]:
                st.markdown(f"- {a_}")

    if plan.get("prerequisites"):
        st.subheader("准备工作")
        for p in plan["prerequisites"]:
            st.markdown(f"- {p}")

    if plan.get("steps"):
        st.subheader("部署步骤")
        for i, step in enumerate(plan["steps"], 1):
            tag = " ⚠️ 需要核对" if step.get("needs_verification") else ""
            with st.container(border=True):
                st.markdown(f"**步骤 {i}：{step['title']}**{tag}")
                if step.get("explanation"):
                    st.caption(step["explanation"])
                for cmd in step.get("commands") or []:
                    st.code(cmd, language="bash")

    if plan.get("verification"):
        st.subheader("跑起来后怎么验证")
        for v in plan["verification"]:
            st.markdown(f"- {v}")

    if plan.get("common_errors"):
        st.subheader("常见报错")
        for e in plan["common_errors"]:
            with st.expander(e["symptom"]):
                if e.get("cause"):
                    st.markdown(f"**原因**：{e['cause']}")
                if e.get("fix"):
                    st.markdown(f"**修复**：{e['fix']}")

    if plan.get("rollback"):
        st.subheader("不想要了怎么回滚")
        for r in plan["rollback"]:
            st.markdown(f"- {r}")

    if plan.get("next_steps"):
        st.subheader("下一步建议")
        for n in plan["next_steps"]:
            st.markdown(f"- {n}")

    fa, fb = st.columns(2)
    if fa.button("✅ 部署成功"):
        _post("/api/feedback", {"full_name": full_name, "action": "deploy_success",
                                "success_status": True})
        st.toast("已记录：部署成功")
    if fb.button("❌ 部署失败"):
        _post("/api/feedback", {"full_name": full_name, "action": "deploy_failed",
                                "success_status": False})
        st.toast("已记录：部署失败")


def page_favorites() -> None:
    st.title("⭐ 我的收藏")
    try:
        favs = _get("/api/favorites") or []
    except Exception as e:
        st.error(str(e))
        return
    if not favs:
        st.info("还没有收藏。在推荐卡片里点 ⭐ 即可加入。")
        return
    st.caption(f"共 {len(favs)} 个收藏")
    for i, f in enumerate(favs):
        repo = f.get("repo") or {}
        full_name = f["full_name"]
        url = repo.get("url") or f"https://github.com/{full_name}"
        with st.container(border=True):
            l, r = st.columns([5, 1])
            with l:
                st.markdown(f"### [{full_name}]({url})")
                if repo.get("description"):
                    st.caption(repo["description"])
                if f.get("last_rationale"):
                    with st.expander("上次的推荐理由"):
                        st.markdown(f["last_rationale"])
            with r:
                st.metric("★", repo.get("stars", "—") if repo else "—")
                st.caption(f"收藏于 {f['favorited_at'][:10]}")

            cols = st.columns([1.2, 1, 4])
            if cols[0].button("📘 生成教程", key=f"fav_tut_{i}", type="primary"):
                try:
                    with st.spinner(f"为 {full_name} 生成教程…"):
                        data = _post("/api/tutorials/generate", {
                            "full_name": full_name,
                            "user_profile": PROFILE,
                        })
                    st.session_state[f"tutorial_{full_name}"] = data
                    st.session_state["selected_repo"] = full_name
                    st.session_state["_pending_nav"] = "Tutorial"
                    st.rerun()
                except RuntimeError as e:
                    st.error(str(e))
            if cols[1].button("🗑 取消收藏", key=f"fav_unfav_{i}"):
                try:
                    _post("/api/feedback", {"full_name": full_name, "action": "unfavorite"})
                    st.toast("已取消")
                    st.rerun()
                except Exception as e:
                    st.error(str(e))


def page_history() -> None:
    st.title("🗂 历史 & 反馈")
    try:
        rows = _get("/api/queries/recent?limit=30")
    except Exception as e:
        st.error(str(e))
        return
    if not rows:
        st.info("还没有历史。")
        return
    st.caption("点击任意一条回到当时的推荐结果；🗑 删除条目（连同其推荐和教程）。")
    for r in rows:
        qid = r["id"]
        raw = r.get("raw_text", "") or "(空)"
        ts = (r.get("created_at") or "")[:19].replace("T", " ")
        cols = _row_columns([7, 2, 1])
        with cols[0]:
            if st.button(f"#{qid} · {raw[:120]}", key=f"hist_{qid}",
                         use_container_width=True):
                st.session_state["_load_history_id"] = qid
                st.session_state["_pending_nav"] = "Recommendations"
                st.rerun()
        cols[1].caption(ts)
        if cols[2].button("🗑", key=f"hist_del_{qid}", help="删除这条历史"):
            try:
                _delete(f"/api/queries/{qid}")
                st.toast(f"已删除 #{qid}")
                st.rerun()
            except Exception as e:
                st.error(str(e))


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


PAGES = {
    "Search": page_search,
    "Recommendations": page_recommendations,
    "Tutorial": page_tutorial,
    "Favorites": page_favorites,
    "History": page_history,
}

if "nav" not in st.session_state:
    st.session_state["nav"] = "Search"

st.sidebar.radio(
    "📍 页面",
    list(PAGES.keys()),
    key="nav",                       # bind directly to session_state["nav"]
    format_func=lambda k: {
        "Search": "需求检索",
        "Recommendations": "推荐结果",
        "Tutorial": "部署教程",
        "Favorites": "我的收藏",
        "History": "历史 & 反馈",
    }[k],
)
PAGES[st.session_state["nav"]]()
