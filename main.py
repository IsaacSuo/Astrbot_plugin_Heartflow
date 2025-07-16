import json
import time
import datetime
from typing import Dict
from dataclasses import dataclass

import astrbot.api.star as star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api import logger

# 导入消息聚合模块
from .message_aggregation import DirectProcessor, AggregationProcessor


@dataclass
class JudgeResult:
    """判断结果数据类"""
    relevance: float = 0.0
    willingness: float = 0.0
    social: float = 0.0
    timing: float = 0.0
    reasoning: str = ""
    should_reply: bool = False
    confidence: float = 0.0
    overall_score: float = 0.0
    related_messages: list = None

    def __post_init__(self):
        if self.related_messages is None:
            self.related_messages = []


@dataclass
class ChatState:
    """群聊状态数据类"""
    energy: float = 1.0
    last_reply_time: float = 0.0
    last_reset_date: str = ""
    total_messages: int = 0
    total_replies: int = 0



class HeartflowPlugin(star.Star):

    def __init__(self, context: star.Context, config):
        super().__init__(context)
        self.config = config

        # 判断模型配置
        self.judge_provider_name = self.config.get("judge_provider_name", "")

        # 心流参数配置
        self.reply_threshold = self.config.get("reply_threshold", 0.6)
        self.energy_decay_rate = self.config.get("energy_decay_rate", 0.1)
        self.energy_recovery_rate = self.config.get("energy_recovery_rate", 0.02)
        self.context_messages_count = self.config.get("context_messages_count", 5)
        self.whitelist_enabled = self.config.get("whitelist_enabled", False)
        self.chat_whitelist = self.config.get("chat_whitelist", [])

        # 群聊状态管理
        self.chat_states: Dict[str, ChatState] = {}

        # 判断权重配置
        self.weights = {
            "relevance": 0.3,
            "willingness": 0.25,
            "social": 0.25,
            "timing": 0.2
        }

        # 初始化消息处理器
        self._init_message_processor()

        logger.info("心流插件已初始化")

    def _init_message_processor(self):
        """初始化消息处理器"""
        # 根据配置选择消息处理器
        if self.config.get("enable_message_aggregation", False):
            # 创建聚合处理器
            self.message_processor = AggregationProcessor(
                config=self.config,
                judge_provider_getter=self._get_judge_provider
            )
            logger.info("✅ 消息聚合处理器已启用")
        else:
            # 创建直接处理器
            self.message_processor = DirectProcessor()
            logger.info("✅ 直接消息处理器已启用")

    def _get_judge_provider(self):
        """获取判断提供商的辅助方法"""
        try:
            if not self.judge_provider_name:
                return None
            return self.context.get_provider_by_id(self.judge_provider_name)
        except Exception as e:
            logger.debug(f"获取判断提供商失败: {e}")
            return None

    async def judge_with_tiny_model(self, event: AstrMessageEvent) -> JudgeResult:
        """使用小模型进行智能判断"""

        if not self.judge_provider_name:
            logger.warning("小参数判断模型提供商名称未配置，跳过心流判断")
            return JudgeResult(should_reply=False, reasoning="提供商未配置")

        # 获取指定的 provider
        try:
            judge_provider = self.context.get_provider_by_id(self.judge_provider_name)
            if not judge_provider:
                logger.warning(f"未找到提供商: {self.judge_provider_name}")
                return JudgeResult(should_reply=False, reasoning=f"提供商不存在: {self.judge_provider_name}")
        except Exception as e:
            logger.error(f"获取提供商失败: {e}")
            return JudgeResult(should_reply=False, reasoning=f"获取提供商失败: {str(e)}")

        # 获取群聊状态
        chat_state = self._get_chat_state(event.unified_msg_origin)

        # 获取当前对话的人格系统提示词，让模型了解大参数LLM的角色设定
        persona_system_prompt = await self._get_persona_system_prompt(event)
        logger.debug(f"小参数模型获取人格提示词: {'有' if persona_system_prompt else '无'} | 长度: {len(persona_system_prompt) if persona_system_prompt else 0}")

        # 构建判断上下文
        chat_context = await self._build_chat_context(event)
        recent_messages = await self._get_recent_messages(event)

        judge_prompt = f"""
你是群聊机器人的决策系统，需要判断是否应该主动回复以下消息。

## 机器人角色设定
{persona_system_prompt if persona_system_prompt else "默认角色：智能助手"}

## 当前群聊情况
- 群聊ID: {event.unified_msg_origin}
- 我的精力水平: {chat_state.energy:.1f}/1.0
- 上次发言: {self._get_minutes_since_last_reply(event.unified_msg_origin)}分钟前

## 群聊基本信息
{chat_context}

## 最近{self.context_messages_count}条对话历史
{recent_messages}

## 待判断消息
发送者: {event.get_sender_name()}
内容: {event.message_str}
时间: {datetime.datetime.now().strftime('%H:%M:%S')}

## 评估要求
请从以下4个维度评估（0-10分），**重要提醒：基于上述机器人角色设定来判断是否适合回复**：

1. **内容相关度**(0-10)：消息是否有趣、有价值、适合我回复
   - 考虑消息的质量、话题性、是否需要回应
   - 识别并过滤垃圾消息、无意义内容
   - **结合机器人角色特点，判断是否符合角色定位**

2. **回复意愿**(0-10)：基于当前状态，我回复此消息的意愿
   - 考虑当前精力水平和心情状态
   - 考虑今日回复频率控制
   - **基于机器人角色设定，判断是否应该主动参与此话题**

3. **社交适宜性**(0-10)：在当前群聊氛围下回复是否合适
   - 考虑群聊活跃度和讨论氛围
   - **考虑机器人角色在群中的定位和表现方式**

4. **时机恰当性**(0-10)：回复时机是否恰当
   - 考虑距离上次回复的时间间隔
   - 考虑消息的紧急性和时效性

**回复阈值**: {self.reply_threshold} (综合评分达到此分数才回复)

**关联消息筛选要求**：
- 从上面的对话历史中找出与当前消息内容相关的消息
- 直接复制相关消息的完整内容，保持原有格式
- 如果没有相关消息，返回空数组

请以JSON格式回复：
{{
    "relevance": 分数,
    "willingness": 分数,
    "social": 分数,
    "timing": 分数,
    "reasoning": "详细分析原因，说明为什么应该或不应该回复，需要结合机器人角色特点进行分析",
    "should_reply": true/false,
    "confidence": 0.0-1.0,
    "related_messages": ["从上面对话历史中筛选出与当前消息可能有关联的消息，直接复制完整内容保持原格式，如果没有关联消息则为空数组"]
}}
"""

        try:
            # 使用 provider 调用模型，传入最近的对话历史作为上下文
            recent_contexts = await self._get_recent_contexts(event)

            # 构建完整的判断提示词，将系统提示直接整合到prompt中
            complete_judge_prompt = "你是一个专业的群聊回复决策系统，能够准确判断消息价值和回复时机。"
            if persona_system_prompt:
                complete_judge_prompt += f"\n\n你正在为以下角色的机器人做决策：\n{persona_system_prompt}"
            complete_judge_prompt += "\n\n请严格按照JSON格式返回结果，不要包含其他内容。\n\n"
            complete_judge_prompt += judge_prompt

            llm_response = await judge_provider.text_chat(
                prompt=complete_judge_prompt,
                contexts=recent_contexts  # 传入最近的对话历史
            )

            content = llm_response.completion_text.strip()

            # 尝试提取JSON
            try:
                if content.startswith("```json"):
                    content = content.replace("```json", "").replace("```", "").strip()
                elif content.startswith("```"):
                    content = content.replace("```", "").strip()

                judge_data = json.loads(content)

                # 计算综合评分
                overall_score = (
                    judge_data.get("relevance", 0) * self.weights["relevance"] +
                    judge_data.get("willingness", 0) * self.weights["willingness"] +
                    judge_data.get("social", 0) * self.weights["social"] +
                    judge_data.get("timing", 0) * self.weights["timing"]
                ) / 10.0

                return JudgeResult(
                    relevance=judge_data.get("relevance", 0),
                    willingness=judge_data.get("willingness", 0),
                    social=judge_data.get("social", 0),
                    timing=judge_data.get("timing", 0),
                    reasoning=judge_data.get("reasoning", ""),
                    should_reply=judge_data.get("should_reply", False) and overall_score >= self.reply_threshold,
                    confidence=judge_data.get("confidence", 0.0),
                    overall_score=overall_score,
                    related_messages=judge_data.get("related_messages", [])
                )
            except json.JSONDecodeError as e:
                logger.error(f"小参数模型返回非有效JSON: {content}")
                return JudgeResult(should_reply=False, reasoning=f"JSON解析失败: {str(e)}")

        except Exception as e:
            logger.error(f"小参数模型判断异常: {e}")
            return JudgeResult(should_reply=False, reasoning=f"异常: {str(e)}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def on_group_message(self, event: AstrMessageEvent):
        """群聊消息处理入口"""

        # 检查基本条件
        if not self._should_process_message(event):
            return

        try:
            # 通过消息处理器处理消息
            processed_event = await self.message_processor.process_message(event)
            
            # 如果处理器返回None，说明正在等待聚合或消息被过滤
            if processed_event is None:
                return
            
            # 进行心流判断（与原有逻辑完全一致）
            judge_result = await self.judge_with_tiny_model(processed_event)
            
            # 处理判断结果
            async for result in self._handle_judge_result(processed_event, judge_result):
                yield result

        except Exception as e:
            logger.error(f"心流插件处理消息异常: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def _handle_judge_result(self, event: AstrMessageEvent, judge_result: JudgeResult):
        """处理心流判断结果"""
        if judge_result.should_reply:
            logger.info(f"🔥 心流触发主动回复 | {event.unified_msg_origin[:20]}... | 评分:{judge_result.overall_score:.2f}")

            # 生成主动回复
            try:
                result_count = 0
                async for result in self._generate_active_reply(event, judge_result):
                    result_count += 1
                    logger.debug(f"心流回复生成器产生结果 #{result_count}: {type(result)}")
                    yield result

            except Exception as e:
                logger.error(f"执行心流回复生成器异常: {e}")
                self._update_passive_state(event, judge_result)
        else:
            # 记录被动状态
            logger.debug(f"心流判断不通过 | {event.unified_msg_origin[:20]}... | 评分:{judge_result.overall_score:.2f} | 原因: {judge_result.reasoning[:30]}...")
            self._update_passive_state(event, judge_result)

    async def _generate_active_reply(self, event: AstrMessageEvent, judge_result: JudgeResult):
        """生成主动回复"""

        # 获取当前对话信息
        curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        conversation = None
        context = []

        if curr_cid:
            conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
            if conversation and conversation.history:
                context = json.loads(conversation.history)

        # 获取当前对话的人格系统提示词
        system_prompt = await self._get_persona_system_prompt(event)

        # 构建简化的回复提示词
        related_messages_text = ""
        if judge_result.related_messages:
            related_messages_text = "\n".join(judge_result.related_messages)
        else:
            related_messages_text = "无相关历史消息"

        enhanced_prompt = f"""**当前消息**：
{event.get_sender_name()}: {event.message_str}

**历史消息中可能有关联的消息**：
{related_messages_text}
"""

        func_tools_mgr = self.context.get_llm_tool_manager()

        try:
            request_obj = event.request_llm(
                prompt=enhanced_prompt,
                func_tool_manager=func_tools_mgr,
                session_id=curr_cid,
                contexts=context,
                system_prompt=system_prompt,
                image_urls=[],
                conversation=conversation
            )

            logger.debug(f"心流插件创建ProviderRequest: {type(request_obj)} | prompt长度: {len(enhanced_prompt)} | 系统提示词长度: {len(system_prompt) if system_prompt else 0}")
            yield request_obj

            # 更新主动回复状态
            self._update_active_state(event, judge_result)
            logger.info(f"💖 心流主动回复请求已提交 | {event.unified_msg_origin[:20]}... | 评分:{judge_result.overall_score:.2f} | {judge_result.reasoning[:50]}...")

        except Exception as e:
            logger.error(f"生成心流回复失败: {e}")
            yield event.plain_result("生成回复时出现错误，请稍后再试。")
            self._update_active_state(event, judge_result)

    def _should_process_message(self, event: AstrMessageEvent) -> bool:
        """检查是否应该处理这条消息"""

        # 检查插件是否启用
        if not self.config.get("enable_heartflow", False):
            return False

        # 跳过指令消息
        if event.is_at_or_wake_command:
            logger.debug(f"跳过指令消息: {event.message_str}")
            return False

        # 检查白名单
        if self.whitelist_enabled:
            if not self.chat_whitelist:
                logger.debug(f"白名单为空，跳过处理: {event.unified_msg_origin}")
                return False

            if event.unified_msg_origin not in self.chat_whitelist:
                logger.debug(f"群聊不在白名单中，跳过处理: {event.unified_msg_origin}")
                return False

        # 跳过机器人自己的消息
        if event.get_sender_id() == event.get_self_id():
            return False

        # 跳过空消息
        if not event.message_str or not event.message_str.strip():
            return False


        return True

    def _get_chat_state(self, chat_id: str) -> ChatState:
        """获取群聊状态"""
        if chat_id not in self.chat_states:
            self.chat_states[chat_id] = ChatState()

        # 检查日期重置
        today = datetime.date.today().isoformat()
        state = self.chat_states[chat_id]

        if state.last_reset_date != today:
            state.last_reset_date = today
            # 每日重置时恢复一些精力
            state.energy = min(1.0, state.energy + 0.2)

        return state

    def _get_minutes_since_last_reply(self, chat_id: str) -> int:
        """获取距离上次回复的分钟数"""
        chat_state = self._get_chat_state(chat_id)

        if chat_state.last_reply_time == 0:
            return 999  # 从未回复过

        return int((time.time() - chat_state.last_reply_time) / 60)

    async def _get_recent_contexts(self, event: AstrMessageEvent) -> list:
        """获取最近的对话上下文（用于传递给LLM）"""
        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            if not curr_cid:
                return []

            conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
            if not conversation or not conversation.history:
                return []

            context = json.loads(conversation.history)

            # 获取最近的 context_messages_count 条消息
            recent_context = context[-self.context_messages_count:] if len(context) > self.context_messages_count else context

            return recent_context

        except Exception as e:
            logger.debug(f"获取对话上下文失败: {e}")
            return []

    async def _build_chat_context(self, event: AstrMessageEvent) -> str:
        """构建群聊上下文"""
        chat_state = self._get_chat_state(event.unified_msg_origin)

        context_info = f"""最近活跃度: {'高' if chat_state.total_messages > 100 else '中' if chat_state.total_messages > 20 else '低'}
历史回复率: {(chat_state.total_replies / max(1, chat_state.total_messages) * 100):.1f}%
当前时间: {datetime.datetime.now().strftime('%H:%M')}"""
        return context_info

    async def _get_recent_messages(self, event: AstrMessageEvent) -> str:
        """获取最近的消息历史（用于小参数模型判断）"""
        try:
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            if not curr_cid:
                return "暂无对话历史"

            conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
            if not conversation or not conversation.history:
                return "暂无对话历史"

            context = json.loads(conversation.history)

            # 获取最近的 context_messages_count 条消息
            recent_context = context[-self.context_messages_count:] if len(context) > self.context_messages_count else context

            # 直接返回原始的对话历史，让小参数模型自己判断
            messages_text = []
            for msg in recent_context:
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if role in ["user", "assistant"]:
                    messages_text.append(content)

            return "\n---\n".join(messages_text) if messages_text else "暂无对话历史"

        except Exception as e:
            logger.debug(f"获取消息历史失败: {e}")
            return "暂无对话历史"

    def _update_active_state(self, event: AstrMessageEvent, judge_result: JudgeResult):
        """更新主动回复状态"""
        chat_id = event.unified_msg_origin
        chat_state = self._get_chat_state(chat_id)

        # 更新回复相关状态
        chat_state.last_reply_time = time.time()
        chat_state.total_replies += 1
        chat_state.total_messages += 1

        # 精力消耗（回复后精力下降）
        chat_state.energy = max(0.1, chat_state.energy - self.energy_decay_rate)

        logger.debug(f"更新主动状态: {chat_id[:20]}... | 精力: {chat_state.energy:.2f}")

    def _update_passive_state(self, event: AstrMessageEvent, judge_result: JudgeResult):
        """更新被动状态（未回复）"""
        chat_id = event.unified_msg_origin
        chat_state = self._get_chat_state(chat_id)

        # 更新消息计数
        chat_state.total_messages += 1

        # 精力恢复（不回复时精力缓慢恢复）
        chat_state.energy = min(1.0, chat_state.energy + self.energy_recovery_rate)

        logger.debug(f"更新被动状态: {chat_id[:20]}... | 精力: {chat_state.energy:.2f} | 原因: {judge_result.reasoning[:30]}...")

    # 管理员命令：查看心流状态
    @filter.command("heartflow")
    async def heartflow_status(self, event: AstrMessageEvent):
        """查看心流状态"""

        chat_id = event.unified_msg_origin
        chat_state = self._get_chat_state(chat_id)

        # 获取聚合状态（如果启用）
        aggregation_info = ""
        if hasattr(self.message_processor, 'get_aggregation_status'):
            agg_status = self.message_processor.get_aggregation_status()
            aggregation_info = f"""
📮 **消息聚合状态**
- 聚合功能: ✅ 已启用
- 当前聚合数: {agg_status['waiting_aggregations']}/{agg_status['total_aggregations']}
- 最大等待时间: {agg_status['max_wait_time']}秒
- 信心阈值: {agg_status['confidence_threshold']}
"""
        else:
            aggregation_info = """
📮 **消息聚合状态**
- 聚合功能: ❌ 未启用
"""

        status_info = f"""
🔮 心流状态报告

📊 **当前状态**
- 群聊ID: {event.unified_msg_origin}
- 精力水平: {chat_state.energy:.2f}/1.0 {'🟢' if chat_state.energy > 0.7 else '🟡' if chat_state.energy > 0.3 else '🔴'}
- 上次回复: {self._get_minutes_since_last_reply(chat_id)}分钟前

📈 **历史统计**
- 总消息数: {chat_state.total_messages}
- 总回复数: {chat_state.total_replies}
- 回复率: {(chat_state.total_replies / max(1, chat_state.total_messages) * 100):.1f}%
{aggregation_info}
⚙️ **配置参数**
- 回复阈值: {self.reply_threshold}
- 判断提供商: {self.judge_provider_name}
- 白名单模式: {'✅ 开启' if self.whitelist_enabled else '❌ 关闭'}
- 白名单群聊数: {len(self.chat_whitelist) if self.whitelist_enabled else 0}

🎯 **插件状态**: {'✅ 已启用' if self.config.get('enable_heartflow', False) else '❌ 已禁用'}
"""

        event.set_result(event.plain_result(status_info))

    # 管理员命令：重置心流状态
    @filter.command("heartflow_reset")
    async def heartflow_reset(self, event: AstrMessageEvent):
        """重置心流状态"""

        chat_id = event.unified_msg_origin
        if chat_id in self.chat_states:
            del self.chat_states[chat_id]

        event.set_result(event.plain_result("✅ 心流状态已重置"))
        logger.info(f"心流状态已重置: {chat_id}")

    # 管理员命令：清理聚合状态
    @filter.command("heartflow_cleanup")
    async def heartflow_cleanup(self, event: AstrMessageEvent):
        """清理过期的消息聚合状态"""
        
        if hasattr(self.message_processor, 'cleanup_expired_aggregations'):
            self.message_processor.cleanup_expired_aggregations()
            event.set_result(event.plain_result("✅ 已清理过期的消息聚合状态"))
            logger.info("已手动清理过期的消息聚合状态")
        else:
            event.set_result(event.plain_result("❌ 当前未启用消息聚合功能"))

    # 管理员命令：切换聚合模式
    @filter.command("heartflow_toggle_aggregation")
    async def heartflow_toggle_aggregation(self, event: AstrMessageEvent):
        """动态切换消息聚合模式"""
        
        current_enabled = self.config.get("enable_message_aggregation", False)
        new_enabled = not current_enabled
        
        # 更新配置
        self.config["enable_message_aggregation"] = new_enabled
        
        # 重新初始化消息处理器
        self._init_message_processor()
        
        status = "启用" if new_enabled else "禁用"
        event.set_result(event.plain_result(f"✅ 消息聚合功能已{status}"))
        logger.info(f"消息聚合功能已{status}")

    async def _get_persona_system_prompt(self, event: AstrMessageEvent) -> str:
        """获取当前对话的人格系统提示词"""
        try:
            # 获取当前对话
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            if not curr_cid:
                # 如果没有对话ID，使用默认人格
                default_persona_name = self.context.provider_manager.selected_default_persona["name"]
                return self._get_persona_prompt_by_name(default_persona_name)

            conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
            if not conversation:
                # 如果没有对话对象，使用默认人格
                default_persona_name = self.context.provider_manager.selected_default_persona["name"]
                return self._get_persona_prompt_by_name(default_persona_name)

            # 获取人格ID
            persona_id = conversation.persona_id

            if not persona_id:
                # persona_id 为 None 时，使用默认人格
                persona_id = self.context.provider_manager.selected_default_persona["name"]
            elif persona_id == "[%None]":
                # 用户显式取消人格时，不使用任何人格
                return ""

            return self._get_persona_prompt_by_name(persona_id)

        except Exception as e:
            logger.debug(f"获取人格系统提示词失败: {e}")
            return ""

    def _get_persona_prompt_by_name(self, persona_name: str) -> str:
        """根据人格名称获取人格提示词"""
        try:
            # 从provider_manager中查找人格
            for persona in self.context.provider_manager.personas:
                if persona["name"] == persona_name:
                    return persona.get("prompt", "")

            logger.debug(f"未找到人格: {persona_name}")
            return ""

        except Exception as e:
            logger.debug(f"获取人格提示词失败: {e}")
            return ""
