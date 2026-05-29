from dotenv import load_dotenv
import os
import json
import asyncio
from langchain.chat_models import init_chat_model
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, AIMessage, AIMessageChunk, SystemMessage
from tools import (
    get_current_weather,
    search_knowledge_base,
    search_drug_instruction,
    search_user_case,
    search_medical_kg,
    get_last_rag_context,
    reset_tool_call_guards,
    set_rag_step_queue,
    set_tool_user_context,
)
from datetime import datetime
from cache import cache
from database import SessionLocal
from models import User, ChatSession, ChatMessage, HealthProfile, MedicalCase

load_dotenv()

API_KEY = os.getenv("ARK_API_KEY")
MODEL = os.getenv("MODEL")
BASE_URL = os.getenv("BASE_URL")

class ConversationStorage:
    """对话存储（PostgreSQL + Redis）。"""

    @staticmethod
    def _messages_cache_key(user_id: str, session_id: str) -> str:
        return f"chat_messages:{user_id}:{session_id}"

    @staticmethod
    def _sessions_cache_key(user_id: str) -> str:
        return f"chat_sessions:{user_id}"

    @staticmethod
    def _to_langchain_messages(records: list[dict]) -> list:
        messages = []
        for msg_data in records:
            msg_type = msg_data.get("type")
            content = msg_data.get("content", "")
            if msg_type == "human":
                messages.append(HumanMessage(content=content))
            elif msg_type == "ai":
                messages.append(AIMessage(content=content))
            elif msg_type == "system":
                messages.append(SystemMessage(content=content))
        return messages

    def save(self, user_id: str, session_id: str, messages: list, metadata: dict = None, extra_message_data: list = None):
        """保存对话"""
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == user_id).first()
            if not user:
                return

            session = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id, ChatSession.session_id == session_id)
                .first()
            )
            if not session:
                session = ChatSession(user_id=user.id, session_id=session_id, metadata_json=metadata or {})
                db.add(session)
                db.flush()
            else:
                session.metadata_json = metadata or {}

            db.query(ChatMessage).filter(ChatMessage.session_ref_id == session.id).delete(synchronize_session=False)

            serialized = []
            now = datetime.utcnow()
            for idx, msg in enumerate(messages):
                rag_trace = None
                if extra_message_data and idx < len(extra_message_data):
                    extra = extra_message_data[idx] or {}
                    rag_trace = extra.get("rag_trace")

                db.add(
                    ChatMessage(
                        session_ref_id=session.id,
                        message_type=msg.type,
                        content=str(msg.content),
                        timestamp=now,
                        rag_trace=rag_trace,
                    )
                )
                serialized.append(
                    {
                        "type": msg.type,
                        "content": str(msg.content),
                        "timestamp": now.isoformat(),
                        "rag_trace": rag_trace,
                    }
                )

            session.updated_at = now
            db.commit()

            cache.set_json(self._messages_cache_key(user_id, session_id), serialized)
            cache.delete(self._sessions_cache_key(user_id))
        finally:
            db.close()

    def load(self, user_id: str, session_id: str) -> list:
        """加载对话"""
        cached = cache.get_json(self._messages_cache_key(user_id, session_id))
        if cached is not None:
            return self._to_langchain_messages(cached)

        records = self.get_session_messages(user_id, session_id)
        cache.set_json(self._messages_cache_key(user_id, session_id), records)
        return self._to_langchain_messages(records)

    def list_sessions(self, user_id: str) -> list:
        """列出用户的所有会话"""
        return [item["session_id"] for item in self.list_session_infos(user_id)]

    def list_session_infos(self, user_id: str) -> list[dict]:
        cached = cache.get_json(self._sessions_cache_key(user_id))
        if cached is not None:
            return cached

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == user_id).first()
            if not user:
                return []

            sessions = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id)
                .order_by(ChatSession.updated_at.desc())
                .all()
            )
            result = []
            for s in sessions:
                count = db.query(ChatMessage).filter(ChatMessage.session_ref_id == s.id).count()
                result.append(
                    {
                        "session_id": s.session_id,
                        "updated_at": s.updated_at.isoformat(),
                        "message_count": count,
                    }
                )
            cache.set_json(self._sessions_cache_key(user_id), result)
            return result
        finally:
            db.close()

    def get_session_messages(self, user_id: str, session_id: str) -> list[dict]:
        cached = cache.get_json(self._messages_cache_key(user_id, session_id))
        if cached is not None:
            return cached

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == user_id).first()
            if not user:
                return []
            session = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id, ChatSession.session_id == session_id)
                .first()
            )
            if not session:
                return []

            rows = (
                db.query(ChatMessage)
                .filter(ChatMessage.session_ref_id == session.id)
                .order_by(ChatMessage.id.asc())
                .all()
            )
            result = [
                {
                    "type": row.message_type,
                    "content": row.content,
                    "timestamp": row.timestamp.isoformat(),
                    "rag_trace": row.rag_trace,
                }
                for row in rows
            ]
            cache.set_json(self._messages_cache_key(user_id, session_id), result)
            return result
        finally:
            db.close()

    def delete_session(self, user_id: str, session_id: str) -> bool:
        """删除指定用户的会话，返回是否删除成功"""
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == user_id).first()
            if not user:
                return False
            session = (
                db.query(ChatSession)
                .filter(ChatSession.user_id == user.id, ChatSession.session_id == session_id)
                .first()
            )
            if not session:
                return False

            db.delete(session)
            db.commit()
            cache.delete(self._messages_cache_key(user_id, session_id))
            cache.delete(self._sessions_cache_key(user_id))
            return True
        finally:
            db.close()


class HealthProfileStorage:
    """用户健康档案摘要。只记录用户主动提供或上传资料中出现的信息。"""

    @staticmethod
    def _cache_key(user_id: str) -> str:
        return f"health_profile:{user_id}"

    def load(self, user_id: str) -> str:
        cached = cache.get_json(self._cache_key(user_id))
        if isinstance(cached, dict):
            return cached.get("summary", "")

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == user_id).first()
            if not user:
                return ""
            profile = db.query(HealthProfile).filter(HealthProfile.user_id == user.id).first()
            summary = profile.summary if profile else ""
            cache.set_json(self._cache_key(user_id), {"summary": summary})
            return summary
        finally:
            db.close()

    def save(self, user_id: str, summary: str) -> None:
        summary = (summary or "").strip()
        if not summary:
            return

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.username == user_id).first()
            if not user:
                return
            profile = db.query(HealthProfile).filter(HealthProfile.user_id == user.id).first()
            if profile:
                profile.summary = summary
                profile.updated_at = datetime.utcnow()
            else:
                db.add(HealthProfile(user_id=user.id, summary=summary, updated_at=datetime.utcnow()))
            db.commit()
            cache.set_json(self._cache_key(user_id), {"summary": summary})
        finally:
            db.close()



def create_agent_instance():
    model = init_chat_model(
        model=MODEL,
        model_provider="openai",
        api_key=API_KEY,
        base_url=BASE_URL,
        temperature=0.3,
        stream_usage=True,
    )

    agent = create_agent(
        model=model,
        tools=[
            get_current_weather,
            search_knowledge_base,
            search_drug_instruction,
            search_user_case,
            search_medical_kg,
        ],
        system_prompt=(
            "You are a medical education and medication-safety Agent. "
            "Speak warmly and naturally like a careful human assistant: greet the user when appropriate, "
            "acknowledge their concern, ask one or two necessary follow-up questions, and avoid sounding like a database. "
            "Your role is to help with health education, drug-instruction lookup, and doctor-visit preparation; "
            "you must not diagnose, prescribe, or replace a clinician. "
            "Remember and use the user's health profile when provided, including symptoms, uploaded reports, medications, allergies, "
            "medical history, and follow-up plans. If you use remembered information, mention it carefully and invite correction. "
            "Use search_medical_kg for questions about diseases, symptoms, checks, departments, foods, drugs, treatment methods, "
            "or medical knowledge graph facts. "
            "Use search_drug_instruction when users ask about uploaded drug instructions or medication documents. "
            "When the user only asks to summarize or explain a drug instruction, analyze the drug instruction alone; "
            "do not infer or attach a personal case unless the user explicitly asks to combine it with their condition. "
            "Use search_user_case when users ask about their uploaded medical records, check reports, or personal case materials. "
            "When the user only asks to summarize or explain a case/check report, analyze the personal case alone; "
            "do not bring in a drug instruction unless the user names a drug or asks about medication safety. "
            "For medication-safety questions that explicitly name a case title and combine that case with a drug, such as "
            "'结合病例甲', '根据病例甲', '病例甲能不能用这个药', or similar wording, use both search_user_case and "
            "search_drug_instruction when possible: first understand the user's case context, then compare it with the drug "
            "instruction's indications, dosage, contraindications, adverse reactions, precautions, interactions, pregnancy/children/elderly "
            "notes, and allergy warnings. Clearly separate: 1) facts from the user's case, 2) facts from the drug instruction, "
            "3) crossed medication-safety concerns, and 4) questions that need confirmation by a clinician or pharmacist. "
            "If the user asks to combine personal circumstances with a drug but does not name a case title, ask which case to use; "
            "do not search all personal cases. "
            "Use search_knowledge_base for general uploaded document knowledge when the source type is unclear. "
            "Use get_current_weather only for weather questions. "
            "When medical information is retrieved, explain it clearly in Chinese, cite the retrieved facts, and include a brief safety reminder. "
            "Do not call the same tool repeatedly in one turn. After receiving a sufficient tool result, produce the final answer directly. "
            "If retrieved context is insufficient, say so honestly and suggest what information the user can provide next. "
            "Do not reveal chain-of-thought."
        ),
    )
    return agent, model


agent = None
model = None


def get_agent_model():
    global agent, model
    if agent is None or model is None:
        agent, model = create_agent_instance()
    return agent, model

storage = ConversationStorage()
health_profile_storage = HealthProfileStorage()


def _user_case_titles(user_id: str) -> list[str]:
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == user_id).first()
        if not user:
            return []
        rows = db.query(MedicalCase).filter(MedicalCase.user_id == user.id).all()
        return [row.title for row in rows if row.title]
    finally:
        db.close()


def _case_title_mentioned(text: str, title: str) -> bool:
    if not title:
        return False
    if len(title) > 1:
        return title in text
    patterns = [
        f"病例{title}",
        f"病历{title}",
        f"档案{title}",
        f"{title}病例",
        f"{title}病历",
        f"结合{title}",
        f"根据{title}",
    ]
    return any(pattern in text for pattern in patterns)


def _mentioned_case_titles(user_id: str, user_text: str) -> list[str]:
    text = user_text or ""
    return [title for title in _user_case_titles(user_id) if _case_title_mentioned(text, title)]


def _is_personal_followup_query(user_text: str) -> bool:
    text = user_text or ""
    terms = [
        "医生",
        "就诊",
        "复诊",
        "沟通",
        "怎么说",
        "问什么",
        "注意",
        "风险",
        "能不能",
        "能吃",
        "可以吃",
        "怎么办",
        "下一步",
        "建议",
        "异常指标",
        "严重吗",
        "要不要",
    ]
    return any(term in text for term in terms)


def _case_scope_from_context(user_id: str, user_text: str, messages: list | None = None) -> tuple[list[str], bool]:
    current = _mentioned_case_titles(user_id, user_text)
    if current:
        return current, False

    if not messages or not _is_personal_followup_query(user_text):
        return [], False

    for msg in reversed(messages[-12:]):
        content = getattr(msg, "content", "")
        if not isinstance(content, str):
            continue
        mentioned = _mentioned_case_titles(user_id, content)
        if len(mentioned) == 1:
            return mentioned, True
    return [], False


def _wants_personal_health_context(user_text: str, user_id: str = "", case_titles: list[str] | None = None) -> bool:
    if case_titles:
        return True
    text = (user_text or "").lower()
    explicit_personal_terms = [
        "健康档案",
        "长期记忆",
        "我的过敏史",
        "我的用药史",
    ]
    return any(term in text for term in explicit_personal_terms)


def _build_query_scope_context(case_titles: list[str], inherited_from_history: bool = False) -> SystemMessage:
    if case_titles:
        source_text = "本轮问题明确点名了病例档案" if not inherited_from_history else "本轮问题未重复点名病例，但当前会话历史最近明确讨论过病例档案"
        content = (
            source_text
            + "："
            + "、".join(case_titles)
            + "。你只可以围绕这些被点名的病例使用健康档案和个人病例资料；"
            "如果同时涉及药品，应将被点名病例与药品说明书交叉分析。"
        )
    else:
        content = (
            "本轮问题没有明确点名任何病例档案。"
            "不要主动使用健康档案、既往病历、已上传病例或 search_user_case；"
            "如果用户是在问药品说明书，请只基于药品说明书分析。"
            "如你认为需要结合个人情况，请先询问用户要结合哪个病例档案，例如“病例甲”，而不是自行代入。"
        )
    return SystemMessage(content=content)


def _build_health_context(user_id: str) -> SystemMessage | None:
    profile = health_profile_storage.load(user_id)
    if not profile:
        return None
    return SystemMessage(
        content=(
            "以下是该用户主动提供或上传资料中整理出的健康档案摘要。"
            "仅当本轮问题明确要求结合用户个人情况、病例、检查单或个人用药安全时才使用。"
            "不要在单纯解释药品说明书或通用医学知识时主动代入这些信息。"
            "不要把摘要当成最终诊断；如信息可能过期或不完整，需要向用户确认。\n"
            f"{profile}"
        )
    )


def update_health_profile_after_turn(user_id: str, user_text: str, assistant_text: str) -> None:
    """用模型从最新一轮对话中更新健康档案摘要。失败时静默跳过，不影响聊天。"""
    try:
        _, current_model = get_agent_model()
        old_profile = health_profile_storage.load(user_id)
        prompt = f"""你是医疗助手的健康档案整理器。
请根据“旧健康档案”和“最新对话”，更新用户健康档案摘要。

只记录用户主动提供、上传资料明确出现、或用户确认过的信息。
不要记录推测性诊断；不确定的信息请写成“待确认”。
保留这些类别：基本信息、症状/主诉、既往史、用药、过敏史、检查结果、上传资料、就医计划、待确认问题。
如果最新对话没有新增健康信息，原样输出旧健康档案。
输出中文，简洁分点。

旧健康档案：
{old_profile or "暂无"}

最新用户消息：
{user_text}

最新助手回复：
{assistant_text}

更新后的健康档案："""
        summary = (current_model.invoke(prompt).content or "").strip()
        if summary and summary != "暂无":
            health_profile_storage.save(user_id, summary)
    except Exception:
        pass

def summarize_old_messages(model, messages: list) -> str:
    """将旧消息总结为摘要"""
    # 提取旧对话
    old_conversation = "\n".join([
        f"{'用户' if msg.type == 'human' else 'AI'}: {msg.content}"
        for msg in messages
    ])

    # 生成摘要
    summary_prompt = f"""请总结以下对话的关键信息：

{old_conversation}
总结（包含用户信息、重要事实、待办事项）："""

    summary = model.invoke(summary_prompt).content
    return summary


def chat_with_agent(user_text: str, user_id: str = "default_user", session_id: str = "default_session"):
    """使用 Agent 处理用户消息并返回响应"""
    current_agent, current_model = get_agent_model()
    messages = storage.load(user_id, session_id)
    case_titles, inherited_case = _case_scope_from_context(user_id, user_text, messages)
    set_tool_user_context(user_id, allowed_case_titles=case_titles)
    transient_messages = [_build_query_scope_context(case_titles, inherited_case)]
    health_context = _build_health_context(user_id) if _wants_personal_health_context(user_text, user_id, case_titles) else None
    if health_context:
        transient_messages.append(health_context)
    messages = transient_messages + messages

    # 清理可能残留的 RAG 上下文，避免跨请求污染
    get_last_rag_context(clear=True)
    reset_tool_call_guards()
    
    if len(messages) > 50:
        summary = summarize_old_messages(current_model, messages[:40])

        messages = [
            SystemMessage(content=f"之前的对话摘要：\n{summary}")
        ] + messages[40:]

    messages.append(HumanMessage(content=user_text))
    result = current_agent.invoke(
        {"messages": messages},
        config={"recursion_limit": 8},
    )

    response_content = ""
    if isinstance(result, dict):
        if "output" in result:
            response_content = result["output"]
        elif "messages" in result and result["messages"]:
            msg = result["messages"][-1]
            response_content = getattr(msg, "content", str(msg))
        else:
            response_content = str(result)
    elif hasattr(result, "content"):
        response_content = result.content
    else:
        response_content = str(result)
    
    messages.append(AIMessage(content=response_content))
    save_messages = messages[len(transient_messages):]
    update_health_profile_after_turn(user_id, user_text, response_content)

    rag_context = get_last_rag_context(clear=True)
    rag_trace = rag_context.get("rag_trace") if rag_context else None

    extra_message_data = [None] * (len(save_messages) - 1) + [{"rag_trace": rag_trace}]
    storage.save(user_id, session_id, save_messages, extra_message_data=extra_message_data)

    return {
        "response": response_content,
        "rag_trace": rag_trace,
    }


async def chat_with_agent_stream(user_text: str, user_id: str = "default_user", session_id: str = "default_session"):
    """使用 Agent 处理用户消息并流式返回响应。
    
    架构：使用统一输出队列 + 后台任务，确保 RAG 检索步骤在工具执行期间实时推送，
    而非等待工具完成后才显示。
    """
    current_agent, current_model = get_agent_model()
    messages = storage.load(user_id, session_id)
    case_titles, inherited_case = _case_scope_from_context(user_id, user_text, messages)
    set_tool_user_context(user_id, allowed_case_titles=case_titles)
    transient_messages = [_build_query_scope_context(case_titles, inherited_case)]
    health_context = _build_health_context(user_id) if _wants_personal_health_context(user_text, user_id, case_titles) else None
    if health_context:
        transient_messages.append(health_context)
    messages = transient_messages + messages

    # 清理可能残留的 RAG 上下文
    get_last_rag_context(clear=True)
    reset_tool_call_guards()

    # 统一输出队列：所有事件（content / rag_step）都汇入这里
    output_queue = asyncio.Queue()

    class _RagStepProxy:
        """代理对象：将 emit_rag_step 的原始 step dict 包装后放入统一输出队列。"""
        def put_nowait(self, step):
            output_queue.put_nowait({"type": "rag_step", "step": step})

    set_rag_step_queue(_RagStepProxy())

    if len(messages) > 50:
        summary = summarize_old_messages(current_model, messages[:40])
        messages = [
            SystemMessage(content=f"之前的对话摘要：\n{summary}")
        ] + messages[40:]

    messages.append(HumanMessage(content=user_text))

    full_response = ""

    async def _agent_worker():
        """后台任务：运行 agent 并将内容 chunk 推入输出队列。"""
        nonlocal full_response
        try:
            async for msg, metadata in current_agent.astream(
                {"messages": messages},
                stream_mode="messages",
                config={"recursion_limit": 8},
            ):
                if not isinstance(msg, AIMessageChunk):
                    continue
                if getattr(msg, "tool_call_chunks", None):
                    continue

                content = ""
                if isinstance(msg.content, str):
                    content = msg.content
                elif isinstance(msg.content, list):
                    for block in msg.content:
                        if isinstance(block, str):
                            content += block
                        elif isinstance(block, dict) and block.get("type") == "text":
                            content += block.get("text", "")

                if content:
                    full_response += content
                    await output_queue.put({"type": "content", "content": content})
        except Exception as e:
            await output_queue.put({"type": "error", "content": str(e)})
        finally:
            # 哨兵：通知主循环 agent 已完成
            await output_queue.put(None)

    # 启动后台任务
    agent_task = asyncio.create_task(_agent_worker())

    try:
        # 主循环：持续从统一队列取事件并 yield SSE
        # RAG 步骤在工具执行期间通过 call_soon_threadsafe 实时入队，不需要等 agent 产出 chunk
        while True:
            event = await output_queue.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"
    except GeneratorExit:
        # 客户端断开连接（AbortController）时，FastAPI 会向此生成器抛出 GeneratorExit
        # 我们必须在此处取消后台任务
        agent_task.cancel()
        try:
            await agent_task
        except asyncio.CancelledError:
            pass  # 任务已成功取消
        raise  # 重新抛出 GeneratorExit 以便 FastAPI 正确处理关闭
    finally:
        # 正常结束或异常退出时清理
        set_rag_step_queue(None)
        if not agent_task.done():
             agent_task.cancel()

    # 获取 RAG trace
    rag_context = get_last_rag_context(clear=True)
    rag_trace = rag_context.get("rag_trace") if rag_context else None

    # 发送 trace 信息
    if rag_trace:
        yield f"data: {json.dumps({'type': 'trace', 'rag_trace': rag_trace})}\n\n"

    # 发送结束信号
    yield "data: [DONE]\n\n"

    # 保存对话
    messages.append(AIMessage(content=full_response))
    save_messages = messages[len(transient_messages):]
    update_health_profile_after_turn(user_id, user_text, full_response)
    extra_message_data = [None] * (len(messages) - 1) + [{"rag_trace": rag_trace}]
    save_extra_message_data = [None] * (len(save_messages) - 1) + [{"rag_trace": rag_trace}]
    storage.save(user_id, session_id, save_messages, extra_message_data=save_extra_message_data)
