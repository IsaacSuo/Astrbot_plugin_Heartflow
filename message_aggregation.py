import json
import time
import asyncio
from typing import Dict, Optional, List
from dataclasses import dataclass
from abc import ABC, abstractmethod

from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger


@dataclass
class CompletenessResult:
    """消息完整性判断结果"""
    is_complete: bool = False
    confidence: float = 0.0
    reasoning: str = ""
    suggested_wait_time: float = 5.0  # 建议等待时间（秒）


@dataclass
class MessageAggregation:
    """消息聚合状态"""
    user_id: str
    chat_id: str
    messages: List[AstrMessageEvent] = None
    aggregated_content: str = ""
    start_time: float = 0.0
    last_message_time: float = 0.0
    timer_task: Optional[asyncio.Task] = None
    is_waiting: bool = False

    def __post_init__(self):
        if self.messages is None:
            self.messages = []


class MessageProcessor(ABC):
    """消息处理器抽象接口"""
    
    @abstractmethod
    async def process_message(self, event: AstrMessageEvent) -> Optional[AstrMessageEvent]:
        """
        处理消息，返回处理后的消息事件
        返回None表示消息正在聚合中，需要等待
        返回AstrMessageEvent表示消息已处理完毕，可以进行心流判断
        """
        pass


class DirectProcessor(MessageProcessor):
    """直接处理器（不进行消息聚合）"""
    
    async def process_message(self, event: AstrMessageEvent) -> Optional[AstrMessageEvent]:
        """直接返回原消息，不进行任何处理"""
        return event


class AggregationProcessor(MessageProcessor):
    """消息聚合处理器"""
    
    def __init__(self, config: dict, judge_provider_getter):
        self.config = config
        self.judge_provider_getter = judge_provider_getter  # 获取判断提供商的函数
        
        # 聚合配置参数
        self.max_wait_time = config.get("aggregation_max_wait_time", 12.0)
        self.confidence_threshold = config.get("aggregation_confidence_threshold", 0.7)
        self.judge_provider_name = config.get("judge_provider_name", "")
        
        # 消息聚合状态管理
        self.aggregations: Dict[str, MessageAggregation] = {}
        
        logger.info(f"消息聚合处理器已初始化 | 最大等待时间: {self.max_wait_time}秒 | 信心阈值: {self.confidence_threshold}")

    def _get_aggregation_key(self, event: AstrMessageEvent) -> str:
        """生成聚合键：群聊ID + 用户ID"""
        return f"{event.unified_msg_origin}_{event.get_sender_id()}"

    async def judge_message_completeness(self, message_content: str, sender_name: str = "") -> CompletenessResult:
        """独立判断消息完整性"""
        
        if not self.judge_provider_name:
            logger.debug("判断提供商未配置，默认认为消息完整")
            return CompletenessResult(is_complete=True, reasoning="提供商未配置，默认完整")
        
        completeness_prompt = f"""
你是一个专业的消息完整性分析系统，请判断以下消息是否表达完整。

## 待分析消息
发送者: {sender_name}
内容: {message_content}

## 判断标准
请分析这条消息是否是一个完整的表达：

**完整消息的特征**：
- 表达了完整的想法或观点
- 语法结构完整，语义明确
- 不存在明显的"未完待续"迹象

**不完整消息的典型特征**：
- 过渡词开头："等等"、"对了"、"还有"、"另外"、"然后"
- 明显的分段表达："我觉得..."（可能还有后续）
- 问题只说了一半
- 语句被截断或有明显的省略
- 引用但未完整说明："刚才说的那个..."
- 表示思考中："让我想想"、"嗯..."
- 列举开始但未完成："首先..."、"第一..."

## 等待时间建议
- 如果判断为不完整，建议等待时间（3-10秒）
- 考虑消息的紧急程度和表达习惯

请以JSON格式回复：
{{
    "is_complete": true/false,
    "confidence": 0.0-1.0,
    "reasoning": "详细分析消息完整性的原因",
    "suggested_wait_time": 等待秒数
}}
"""

        try:
            judge_provider = self.judge_provider_getter()
            if not judge_provider:
                return CompletenessResult(is_complete=True, reasoning="提供商获取失败，默认完整")

            llm_response = await judge_provider.text_chat(prompt=completeness_prompt)
            content = llm_response.completion_text.strip()
            
            # 解析JSON响应
            if content.startswith("```json"):
                content = content.replace("```json", "").replace("```", "").strip()
            elif content.startswith("```"):
                content = content.replace("```", "").strip()

            result_data = json.loads(content)
            
            return CompletenessResult(
                is_complete=result_data.get("is_complete", True),
                confidence=result_data.get("confidence", 0.0),
                reasoning=result_data.get("reasoning", ""),
                suggested_wait_time=min(result_data.get("suggested_wait_time", 5.0), self.max_wait_time)
            )
            
        except Exception as e:
            logger.debug(f"完整性判断失败: {e}")
            return CompletenessResult(is_complete=True, reasoning=f"判断失败，默认完整: {str(e)}")

    async def process_message(self, event: AstrMessageEvent) -> Optional[AstrMessageEvent]:
        """处理消息，可能返回聚合后的消息或None（表示正在等待）"""
        
        aggregation_key = self._get_aggregation_key(event)
        
        # 检查是否有正在进行的聚合
        if aggregation_key in self.aggregations and self.aggregations[aggregation_key].is_waiting:
            # 正在等待中，添加到聚合
            return await self._handle_waiting_aggregation(event, aggregation_key)
        
        # 新消息，先判断完整性
        completeness = await self.judge_message_completeness(event.message_str, event.get_sender_name())
        
        if not completeness.is_complete and completeness.confidence >= self.confidence_threshold:
            # 不完整且判断信心度高，开始聚合等待
            logger.info(f"🔄 检测到不完整消息，开始聚合等待 | {aggregation_key[:30]}... | 等待时间: {completeness.suggested_wait_time}秒 | 信心度: {completeness.confidence:.2f}")
            await self._start_message_aggregation(event, completeness.suggested_wait_time)
            return None  # 返回None表示正在等待
        
        # 消息完整，直接返回
        logger.debug(f"✅ 消息完整，直接处理 | {aggregation_key[:30]}... | 信心度: {completeness.confidence:.2f}")
        return event

    async def _handle_waiting_aggregation(self, event: AstrMessageEvent, aggregation_key: str) -> Optional[AstrMessageEvent]:
        """处理正在等待聚合的消息"""
        
        aggregation = self.aggregations[aggregation_key]
        aggregation.messages.append(event)
        aggregation.last_message_time = time.time()
        aggregation.aggregated_content = " ".join([msg.message_str for msg in aggregation.messages])
        
        # 重新判断完整性
        completeness = await self.judge_message_completeness(aggregation.aggregated_content, event.get_sender_name())
        
        if completeness.is_complete and completeness.confidence >= self.confidence_threshold:
            # 现在完整了，立即处理
            logger.info(f"✅ 消息聚合完成 | {aggregation_key[:30]}... | 消息数: {len(aggregation.messages)} | 最终内容: {aggregation.aggregated_content[:50]}...")
            return await self._finalize_aggregation(aggregation)
        else:
            # 仍不完整，继续等待（计时器会自动处理超时）
            logger.debug(f"⏳ 消息仍不完整，继续等待 | {aggregation_key[:30]}... | 信心度: {completeness.confidence:.2f} | 消息数: {len(aggregation.messages)}")
            return None

    async def _start_message_aggregation(self, event: AstrMessageEvent, wait_time: float):
        """开始消息聚合等待"""
        
        key = self._get_aggregation_key(event)
        
        # 创建聚合状态
        self.aggregations[key] = MessageAggregation(
            user_id=event.get_sender_id(),
            chat_id=event.unified_msg_origin,
            messages=[event],
            aggregated_content=event.message_str,
            start_time=time.time(),
            last_message_time=time.time(),
            is_waiting=True
        )
        
        # 启动等待计时器
        self.aggregations[key].timer_task = asyncio.create_task(
            self._wait_for_completion(key, wait_time)
        )
        
        logger.debug(f"开始消息聚合等待 | {key[:30]}... | 等待时间: {wait_time}秒")

    async def _wait_for_completion(self, aggregation_key: str, wait_time: float):
        """等待消息聚合完成"""
        try:
            await asyncio.sleep(wait_time)
            
            # 超时后处理聚合的消息
            if aggregation_key in self.aggregations:
                aggregation = self.aggregations[aggregation_key]
                if aggregation.is_waiting:
                    logger.info(f"⏰ 聚合等待超时，处理聚合消息 | {aggregation_key[:30]}... | 消息数: {len(aggregation.messages)}")
                    # 注意：这里不能直接调用处理函数，因为我们在异步任务中
                    # 只是标记为完成，实际处理由外部循环处理
                    aggregation.is_waiting = False
                    
        except asyncio.CancelledError:
            logger.debug(f"聚合等待被取消: {aggregation_key[:30]}...")

    async def _finalize_aggregation(self, aggregation: MessageAggregation) -> AstrMessageEvent:
        """完成消息聚合，返回聚合后的消息事件"""
        
        # 取消计时器
        if aggregation.timer_task and not aggregation.timer_task.done():
            aggregation.timer_task.cancel()
        
        # 使用最后一条消息作为基础，修改其内容为聚合内容
        final_event = aggregation.messages[-1]
        final_event.message_str = aggregation.aggregated_content
        
        # 清理聚合状态
        aggregation_key = self._get_aggregation_key(final_event)
        if aggregation_key in self.aggregations:
            del self.aggregations[aggregation_key]
        
        logger.info(f"🔗 消息聚合已完成 | 最终内容: {aggregation.aggregated_content[:50]}...")
        return final_event

    def cleanup_expired_aggregations(self, max_age_seconds: float = 300):
        """清理过期的聚合状态（防止内存泄漏）"""
        current_time = time.time()
        expired_keys = []
        
        for key, aggregation in self.aggregations.items():
            if current_time - aggregation.start_time > max_age_seconds:
                expired_keys.append(key)
                if aggregation.timer_task and not aggregation.timer_task.done():
                    aggregation.timer_task.cancel()
        
        for key in expired_keys:
            del self.aggregations[key]
            logger.debug(f"清理过期聚合状态: {key[:30]}...")

    def get_aggregation_status(self) -> dict:
        """获取聚合状态统计"""
        waiting_count = sum(1 for agg in self.aggregations.values() if agg.is_waiting)
        return {
            "total_aggregations": len(self.aggregations),
            "waiting_aggregations": waiting_count,
            "max_wait_time": self.max_wait_time,
            "confidence_threshold": self.confidence_threshold
        }
