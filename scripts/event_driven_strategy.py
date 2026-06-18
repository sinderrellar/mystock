#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
事件驱动选股策略 v3.0
Event-Driven Stock Selection Strategy

双轨制的"事件轨"：
- 从新闻/公告/政策中识别热点股票（真正的新闻驱动）
- 结合价值面、技术面进行多维度验证
- 捕捉突发事件带来的短期机会

核心改进 (v3.0)：
1. 真正的新闻驱动：从财联社快讯/公告中提取股票代码
2. 多维度综合评分：情绪(30%) + 价值面(35%) + 技术面(35%)
3. 负面情绪过滤：sentiment < -0.3 自动剔除
4. 样本数量验证：news_count < 3 降低权重
5. 公司名称映射：支持从公司名称识别股票代码

数据源：
- 财联社快讯（实时新闻）
- 东方财富个股新闻
- 百度财经政策新闻
- MongoDB 价值面/技术面数据
"""
import os
import sys
import json
import hashlib
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict

import yaml
from anthropic import Anthropic

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)  # 只输出警告，不刷屏


class NewsDataProvider:
    """新闻数据提供者
    
    整合多个新闻数据源：
    - AKShare: 东方财富个股新闻
    - 财联社快讯
    - 公司公告
    """
    
    def __init__(self):
        self._akshare_available = False
        self._sentiment_analyzer = None
        self._check_dependencies()
    
    def _check_dependencies(self):
        """检查依赖"""
        try:
            import akshare as ak
            self._akshare_available = True
            logger.info("✅ AKShare 可用")
        except ImportError:
            logger.warning("⚠️ AKShare 不可用，部分新闻功能受限")
    
    def get_stock_news(self, code: str, limit: int = 10) -> List[Dict]:
        """
        获取个股新闻
        
        Args:
            code: 股票代码（6位数字）
            limit: 新闻数量限制
        
        Returns:
            新闻列表
        """
        news_list = []
        now = datetime.now()

        # 1. 尝试 AKShare 东方财富新闻
        if self._akshare_available:
            try:
                import akshare as ak

                # 东方财富个股新闻（默认按时间倒序）
                df = ak.stock_news_em(symbol=code)

                if df is not None and not df.empty:
                    for _, row in df.iterrows():
                        pub_time_str = str(row.get('发布时间', ''))
                        try:
                            pub_time = datetime.strptime(pub_time_str[:19], "%Y-%m-%d %H:%M:%S")
                        except (ValueError, IndexError):
                            pub_time = now
                        days_old = (now - pub_time).total_seconds() / 86400
                        news_list.append({
                            'title': row.get('新闻标题', ''),
                            'content': row.get('新闻内容', ''),
                            'publish_time': pub_time_str,
                            'days_old': round(days_old, 2),
                            'source': '东方财富',
                            'url': row.get('新闻链接', ''),
                            'stock_code': code
                        })
                        if len(news_list) >= limit:
                            break
                    logger.info(f"获取 {code} 新闻 {len(news_list)} 条 (东方财富)")
            except Exception as e:
                logger.debug(f"东方财富新闻获取失败 ({code}): {e}")

        return news_list

    def get_market_hot_news(self, limit: int = 50) -> List[Dict]:
        """
        获取市场热点新闻
        
        数据源（按优先级）：
        1. stock_news_main_cx - 财新主要新闻
        2. news_economic_baidu - 百度财经新闻
        
        Returns:
            热点新闻列表
        """
        news_list = []
        
        if self._akshare_available:
            try:
                import akshare as ak
                
                # 1. 尝试财新主要新闻 (stock_news_main_cx)
                try:
                    df = ak.stock_news_main_cx()
                    if df is not None and not df.empty:
                        for _, row in df.head(limit).iterrows():
                            news_list.append({
                                'title': row.get('summary', ''),  # 财新用 summary 作为标题
                                'content': row.get('summary', ''),
                                'publish_time': '',
                                'source': '财新',
                                'url': row.get('url', ''),
                                'tag': row.get('tag', '')
                            })
                        logger.info(f"获取财新新闻 {len(news_list)} 条")
                except Exception as e:
                    logger.debug(f"财新新闻获取失败: {e}")
                
                # 2. 如果财新新闻不足，补充百度财经新闻
                if len(news_list) < limit:
                    try:
                        df = ak.news_economic_baidu()
                        if df is not None and not df.empty:
                            remaining = limit - len(news_list)
                            for _, row in df.head(remaining).iterrows():
                                news_list.append({
                                    'title': row.get('title', ''),
                                    'content': row.get('content', ''),
                                    'publish_time': row.get('date', ''),
                                    'source': '百度财经'
                                })
                            logger.info(f"补充百度财经新闻 {remaining} 条")
                    except Exception as e:
                        logger.debug(f"百度财经新闻获取失败: {e}")
                        
            except Exception as e:
                logger.debug(f"市场新闻获取失败: {e}")
        
        return news_list
    
    def get_policy_news(self, limit: int = 30) -> List[Dict]:
        """
        获取政策新闻
        
        Returns:
            政策新闻列表
        """
        news_list = []
        
        if self._akshare_available:
            try:
                import akshare as ak
                
                # 宏观经济新闻
                df = ak.news_economic_baidu()
                
                if df is not None and not df.empty:
                    for _, row in df.head(limit).iterrows():
                        news_list.append({
                            'title': row.get('title', ''),
                            'content': row.get('content', ''),
                            'publish_time': row.get('date', ''),
                            'source': '百度财经',
                            'category': 'policy'
                        })
                    logger.info(f"获取政策新闻 {len(news_list)} 条")
            except Exception as e:
                logger.debug(f"政策新闻获取失败: {e}")
        
        return news_list
    def get_hot_stocks_from_news(self) -> List[Dict]:
        """
        从新闻中提取热点股票（v3.0 真正的新闻驱动）
        
        数据流程：
        1. 获取财联社快讯
        2. 从新闻内容中提取股票代码
        3. 从新闻中识别公司名称并映射到股票代码
        4. 统计每只股票的新闻数量和情绪
        
        Returns:
            热点股票列表，包含股票代码、新闻数量和情绪信息
        """
        hot_stocks = []
        stock_news_map = defaultdict(list)  # code -> [news_list]
        
        if not self._akshare_available:
            logger.warning("⚠️ AKShare 不可用，无法获取新闻数据")
            return hot_stocks
        
        try:
            import akshare as ak
            
            # ========== 1. 获取财新主要新闻 ==========
            news_list = []
            try:
                df = ak.stock_news_main_cx()
                if df is not None and not df.empty:
                    for _, row in df.head(100).iterrows():  # 获取更多新闻
                        news_list.append({
                            'title': row.get('summary', ''),  # 财新用 summary 作为标题
                            'content': row.get('summary', ''),
                            'publish_time': '',
                            'source': '财新',
                            'tag': row.get('tag', '')
                        })
                    logger.info(f"获取财新新闻 {len(news_list)} 条")
            except Exception as e:
                logger.debug(f"财新新闻获取失败: {e}")
            
            # 补充百度财经新闻
            if len(news_list) < 50:
                try:
                    df = ak.news_economic_baidu()
                    if df is not None and not df.empty:
                        for _, row in df.head(50).iterrows():
                            news_list.append({
                                'title': row.get('title', ''),
                                'content': row.get('content', ''),
                                'publish_time': row.get('date', ''),
                                'source': '百度财经'
                            })
                        logger.info(f"补充百度财经新闻 {len(news_list)} 条")
                except Exception as e:
                    logger.debug(f"百度财经新闻获取失败: {e}")
            
            # ========== 2. 获取股票基础信息（用于名称映射）==========
            stock_name_map = {}  # full_name -> code
            short_name_candidates = defaultdict(list)  # short_name -> [codes]
            try:
                df = ak.stock_info_a_code_name()
                if df is not None and not df.empty:
                    for _, row in df.iterrows():
                        code = str(row.get('code', '')).zfill(6)
                        name = row.get('name', '')
                        if name and code:
                            # 存储完整名称（全名唯一，不会碰撞）
                            stock_name_map[name] = code
                            # 收集简称候选（去后缀）
                            short_name = name.replace('股份', '').replace('科技', '').replace('集团', '')
                            if len(short_name) >= 2:
                                short_name_candidates[short_name].append(code)
                    # 只保留无歧义的简称（只映射到一支股票的简称）
                    for short_name, codes in short_name_candidates.items():
                        if len(codes) == 1:
                            stock_name_map[short_name] = codes[0]
                    logger.info(f"加载股票名称映射 {len(stock_name_map)} 条 (含 {len(short_name_candidates)} 个简称候选，{sum(1 for codes in short_name_candidates.values() if len(codes) == 1)} 个无歧义)")
                else:
                    logger.info("加载股票名称映射 0 条")
            except Exception as e:
                logger.debug(f"股票名称映射加载失败: {e}")
            
            # ========== 3. 从新闻中提取股票代码（第一遍：代码提取）==========
            if self._sentiment_analyzer is None:
                self._sentiment_analyzer = NewsSentimentAnalyzer()
            sentiment_analyzer = self._sentiment_analyzer

            # 第一遍：提取股票代码，同时收集文本用于批量情绪分析
            news_texts = []
            news_codes: List[set] = []  # 每条新闻关联的股票代码集合
            for news in news_list:
                text = news.get('title', '') + ' ' + news.get('content', '')
                if not text.strip():
                    news_texts.append("")
                    news_codes.append(set())
                    continue
                news_texts.append(text)

                codes_from_text = sentiment_analyzer.extract_stock_codes(text)
                codes_from_name = []
                for name, code in stock_name_map.items():
                    if len(name) >= 2 and name in text:
                        codes_from_name.append(code)
                news_codes.append(set(codes_from_text + codes_from_name))

            # 批量情绪分析（150 条新闻从 150 次 API 降到 ~5 次）
            sentiments = sentiment_analyzer.analyze_sentiment_batch(news_texts)

            # 第二遍：关联到股票 + 提取主题
            for i, news in enumerate(news_list):
                all_codes = news_codes[i]
                if not all_codes:
                    continue

                sentiment = sentiments[i]
                themes = sentiment_analyzer.extract_themes(news_texts[i])

                for code in all_codes:
                    stock_news_map[code].append({
                        'title': news.get('title', ''),
                        'sentiment': sentiment['score'],
                        'themes': themes,
                        'source': news.get('source', '财联社'),
                        'publish_time': news.get('publish_time', '')
                    })
            
            # ========== 4. 统计并生成热点股票列表 ==========
            for code, news_items in stock_news_map.items():
                if len(news_items) < 1:  # 至少有1条新闻
                    continue
                
                # 计算平均情绪
                avg_sentiment = sum(n['sentiment'] for n in news_items) / len(news_items)
                
                # 收集所有主题
                all_themes = []
                for n in news_items:
                    all_themes.extend(n.get('themes', []))
                unique_themes = list(set(all_themes))
                
                # 获取最新新闻标题作为原因
                latest_news = news_items[0] if news_items else {}
                reason = latest_news.get('title', '新闻提及')[:50]
                
                # 获取股票名称
                stock_name = ''
                for name, c in stock_name_map.items():
                    if c == code:
                        stock_name = name
                        break
                
                hot_stocks.append({
                    'code': code,
                    'name': stock_name,
                    'news_count': len(news_items),
                    'avg_sentiment': round(avg_sentiment, 3),
                    'themes': unique_themes,
                    'source': '新闻提取',
                    'reason': reason,
                    'news_items': news_items[:5]  # 保留最多5条新闻
                })
            
            # 按新闻数量排序（新闻越多，关注度越高）
            hot_stocks.sort(key=lambda x: (x['news_count'], x['avg_sentiment']), reverse=True)
            
            logger.info(f"从新闻中提取热点股票 {len(hot_stocks)} 只")
            
        except Exception as e:
            logger.error(f"从新闻提取热点股票失败: {e}")
            import traceback
            traceback.print_exc()
        
        return hot_stocks


class NewsSentimentAnalyzer:
    """新闻情绪分析器

    使用 LLM 进行语义级情绪分析，能够处理否定、条件、引用等语境。
    """

    def __init__(self, config_path: str = None):
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config", "config_complete.yaml")
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        llm = cfg.get("llm", {})
        self._client = Anthropic(
            base_url=llm.get("base_url", "https://api.deepseek.com/anthropic"),
            api_key=llm.get("api_key", ""),
        )
        self._model = llm.get("model", "deepseek-v4-flash")
        self._timeout = llm.get("timeout", 30)
        self._enabled = bool(llm.get("api_key"))
        self._sentiment_cache: Dict[str, Dict] = {}
        self._max_retries = 2
        self._mongo_sentiment_col = self._init_mongo_cache()

    def _init_mongo_cache(self):
        """初始化 MongoDB 情绪持久化缓存集合。"""
        try:
            from factor_data_import_service import _load_mongodb_config, MongoFactorDataStore, _load_yaml
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config", "config_complete.yaml")
            mongodb_config = _load_mongodb_config(config_path)
            store = MongoFactorDataStore(mongodb_config)
            col = store.db["sentiment_cache"]
            col.create_index("created_at", expireAfterSeconds=7 * 86400)  # 7天TTL自动清理
            return col
        except Exception:
            return None

    def _lookup_mongo_cache(self, keys: List[str]) -> Dict[str, Dict]:
        """从 MongoDB 批量查询缓存结果。"""
        if self._mongo_sentiment_col is None or not keys:
            return {}
        found = {}
        for doc in self._mongo_sentiment_col.find({"_id": {"$in": keys}}):
            found[doc["_id"]] = {
                "score": doc.get("score", 0),
                "sentiment": doc.get("sentiment", "neutral"),
                "positive_keywords": doc.get("positive_keywords", []),
                "negative_keywords": doc.get("negative_keywords", []),
                "key_factors": doc.get("key_factors", []),
                "method": "llm_cached",
            }
        return found

    def _save_mongo_cache(self, key: str, result: Dict) -> None:
        """单条结果持久化到 MongoDB。"""
        if self._mongo_sentiment_col is None:
            return
        try:
            self._mongo_sentiment_col.update_one(
                {"_id": key},
                {"$set": {**result, "created_at": datetime.utcnow()}},
                upsert=True,
            )
        except Exception:
            pass

    SENTIMENT_PROMPT = """你是金融新闻情绪分析器。分析以下新闻标题/摘要，判断其对相关股票的情绪影响。

注意区分：事实 vs 引用（"XX表示"后面的内容才是其观点）、否定 vs 肯定（"否认业绩下滑"是正面）、条件 vs 既成事实（"如果...将..."不是已发生）。

返回JSON（不要markdown包裹）：
{"sentiment": "positive/neutral/negative", "score": -1到1之间的数字, "key_factors": ["关键因素1", "关键因素2"]}

新闻："""

    # 政策主题关键词
    POLICY_THEMES = {
        '新能源': ['新能源', '光伏', '风电', '储能', '锂电', '氢能', '碳中和', '碳达峰'],
        '半导体': ['芯片', '半导体', '集成电路', '国产替代', '先进制程', 'EDA', '光刻'],
        '人工智能': ['AI', '人工智能', '大模型', 'ChatGPT', '算力', 'GPU', '机器学习'],
        '医药医疗': ['创新药', '医疗器械', '生物医药', '集采', '医保', 'CXO'],
        '消费': ['消费升级', '内需', '促消费', '消费券', '零售'],
        '基建': ['基建', '新基建', '铁路', '公路', '水利', '城镇化'],
        '数字经济': ['数字经济', '数据要素', '数字化', '信创', '国产软件'],
        '军工': ['军工', '国防', '航空航天', '军民融合', '装备'],
    }
    
    SENTIMENT_BATCH_PROMPT = """你是金融新闻情绪分析器。对以下每条新闻，判断其对相关股票的情绪影响。

注意区分：事实 vs 引用、否定 vs 肯定、条件 vs 既成事实。

返回JSON数组（不要markdown包裹）：
[{"index": 0, "sentiment": "positive/neutral/negative", "score": -1到1的数字}, ...]

新闻列表：
"""

    @staticmethod
    def _text_hash(text: str) -> str:
        return hashlib.md5(text.encode()).hexdigest()

    def _call_llm(self, prompt: str, max_tokens: int = 150) -> Optional[str]:
        """LLM 调用（带重试），返回响应文本或 None。"""
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=max_tokens,
                    temperature=0,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=self._timeout,
                )
                # DeepSeek 推理模型返回 [ThinkingBlock, TextBlock]，需找 TextBlock
                for block in response.content:
                    if hasattr(block, 'text'):
                        return block.text.strip()
                return None
            except Exception:
                if attempt < self._max_retries:
                    time.sleep(1)
        return None

    def _parse_sentiment_response(self, content: str) -> Dict:
        """解析 LLM 情绪响应，返回标准格式。"""
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("\n", 1)[0]
        result = json.loads(content)
        return {
            "score": round(float(result.get("score", 0)), 3),
            "sentiment": result.get("sentiment", "neutral"),
            "positive_keywords": [],
            "negative_keywords": [],
            "key_factors": result.get("key_factors", []),
            "method": "llm",
        }

    def analyze_sentiment(self, text: str) -> Dict:
        """单条情绪分析（带缓存 + 重试）。"""
        if not text or not self._enabled:
            return {"score": 0, "sentiment": "neutral",
                    "positive_keywords": [], "negative_keywords": [], "method": "none"}

        key = self._text_hash(text)
        if key in self._sentiment_cache:
            return self._sentiment_cache[key]

        content = self._call_llm(self.SENTIMENT_PROMPT + text)
        if content is None:
            return {"score": 0, "sentiment": "neutral",
                    "positive_keywords": [], "negative_keywords": [], "method": "llm_error"}

        result = self._parse_sentiment_response(content)
        self._sentiment_cache[key] = result
        self._save_mongo_cache(key, result)
        return result

    def analyze_sentiment_batch(self, texts: List[str],
                                 batch_size: int = 30) -> List[Dict]:
        """批量情绪分析：合并 LLM 调用 + 缓存去重。

        150 条新闻从 150 次 API 调用降到 ~5 次。
        """
        if not texts or not self._enabled:
            return [{"score": 0, "sentiment": "neutral",
                     "positive_keywords": [], "negative_keywords": [], "method": "none"}
                    for _ in texts]

        results: List[Optional[Dict]] = [None] * len(texts)

        # 先查内存缓存
        uncached: List[Tuple[int, str]] = []
        mongo_lookup_keys: List[str] = []
        for i, text in enumerate(texts):
            if not text.strip():
                results[i] = {"score": 0, "sentiment": "neutral",
                              "positive_keywords": [], "negative_keywords": [], "method": "none"}
                continue
            key = self._text_hash(text)
            if key in self._sentiment_cache:
                results[i] = self._sentiment_cache[key]
            else:
                uncached.append((i, text))
                mongo_lookup_keys.append(key)

        # 再查 MongoDB 持久化缓存
        if mongo_lookup_keys:
            mongo_found = self._lookup_mongo_cache(mongo_lookup_keys)
            still_uncached: List[Tuple[int, str]] = []
            for orig_idx, text in uncached:
                key = self._text_hash(text)
                if key in mongo_found:
                    result = mongo_found[key]
                    results[orig_idx] = result
                    self._sentiment_cache[key] = result  # 回填内存
                else:
                    still_uncached.append((orig_idx, text))
            uncached = still_uncached

        # 批量发给 LLM
        for chunk_start in range(0, len(uncached), batch_size):
            chunk = uncached[chunk_start:chunk_start + batch_size]
            lines = [f"[{idx_in_chunk}] {text}" for idx_in_chunk, (_, text) in enumerate(chunk)]
            prompt = self.SENTIMENT_BATCH_PROMPT + "\n".join(lines)

            content = self._call_llm(prompt, max_tokens=len(chunk) * 80 + 200)
            if content is None:
                # 整批失败，逐条标记 error
                for orig_idx, text in chunk:
                    results[orig_idx] = {"score": 0, "sentiment": "neutral",
                                         "positive_keywords": [], "negative_keywords": [],
                                         "method": "llm_error"}
                continue

            # 解析批量响应
            try:
                if content.startswith("```"):
                    content = content.split("\n", 1)[1].rsplit("\n", 1)[0]
                batch_results = json.loads(content)
                for item in batch_results:
                    idx_in_chunk = int(item.get("index", -1))
                    if 0 <= idx_in_chunk < len(chunk):
                        orig_idx, text = chunk[idx_in_chunk]
                        result = {
                            "score": round(float(item.get("score", 0)), 3),
                            "sentiment": item.get("sentiment", "neutral"),
                            "positive_keywords": [],
                            "negative_keywords": [],
                            "key_factors": item.get("key_factors", []),
                            "method": "llm",
                        }
                        results[orig_idx] = result
                        h_key = self._text_hash(text)
                        self._sentiment_cache[h_key] = result
                        self._save_mongo_cache(h_key, result)
                # 未在响应中的条目标记 error
                for idx_in_chunk, (orig_idx, text) in enumerate(chunk):
                    if results[orig_idx] is None:
                        results[orig_idx] = {"score": 0, "sentiment": "neutral",
                                             "positive_keywords": [], "negative_keywords": [],
                                             "method": "llm_error"}
            except Exception:
                for orig_idx, text in chunk:
                    if results[orig_idx] is None:
                        results[orig_idx] = {"score": 0, "sentiment": "neutral",
                                             "positive_keywords": [], "negative_keywords": [],
                                             "method": "llm_error"}

        return results

    def extract_themes(self, text: str) -> List[str]:
        """
        提取政策主题
        
        Args:
            text: 新闻文本
        
        Returns:
            匹配的主题列表
        """
        if not text:
            return []
        
        text = text.lower()
        matched_themes = []
        
        for theme, keywords in self.POLICY_THEMES.items():
            for keyword in keywords:
                if keyword.lower() in text:
                    if theme not in matched_themes:
                        matched_themes.append(theme)
                    break
        
        return matched_themes
    
    def extract_stock_codes(self, text: str) -> List[str]:
        """
        从文本中提取股票代码
        
        Args:
            text: 新闻文本
        
        Returns:
            股票代码列表
        """
        if not text:
            return []
        
        # 匹配6位数字的股票代码
        pattern = r'\b([036]\d{5})\b'
        codes = re.findall(pattern, text)
        
        return list(set(codes))


class FundamentalValidator:
    """价值面验证器
    
    从 MongoDB 获取基本面数据，验证事件驱动股票的价值面质量
    """
    
    def __init__(self, db=None):
        self.db = db
        self._init_db()
    
    def _init_db(self):
        """初始化数据库连接"""
        if self.db is not None:
            return

        try:
            from factor_data_import_service import _load_mongodb_config, MongoFactorDataStore

            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config", "config_complete.yaml")
            mongodb_config = _load_mongodb_config(config_path)
            self._store = MongoFactorDataStore(mongodb_config)
            self.db = self._store.db
            self._collections = self._store.collections
            logger.info("✅ MongoDB 连接成功 (FundamentalValidator)")
        except Exception as e:
            logger.warning(f"⚠️ MongoDB 连接失败: {e}")
            self.db = None
            self._store = None

    def get_fundamental_data(self, code: str) -> Dict:
        """
        获取股票基本面数据

        Args:
            code: 股票代码（6位数字）

        Returns:
            基本面数据字典
        """
        if self.db is None:
            return {}

        try:
            # 尝试多种代码格式
            code_variants = [
                code,
                f"SH{code}" if code.startswith('6') else f"SZ{code}",
                f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
            ]

            for code_var in code_variants:
                # 从 stock_basic_info 获取基本信息
                basic = self.db[self._collections['basic_info']].find_one({'code': code_var})
                if basic:
                    break

            if not basic:
                return {}

            # 从 stock_financial_data 获取财务数据
            financial = self.db[self._collections['financial_data']].find_one(
                {'code': code_var},
                sort=[('report_period', -1)]
            )
            
            return {
                'pe': basic.get('pe', 0) or 0,
                'pb': basic.get('pb', 0) or 0,
                'ps': basic.get('ps', 0) or 0,
                'market_cap': basic.get('market_cap', 0) or 0,
                'roe': financial.get('roe', 0) if financial else 0,
                'roa': financial.get('roa', 0) if financial else 0,
                'gross_margin': financial.get('gross_margin', 0) if financial else 0,
                'debt_ratio': financial.get('debt_ratio', 0) if financial else 0,
                'revenue_growth': financial.get('revenue_growth', 0) if financial else 0,
                'profit_growth': financial.get('profit_growth', 0) if financial else 0,
            }
        except Exception as e:
            logger.debug(f"获取基本面数据失败 ({code}): {e}")
            return {}
    
    def calculate_value_score(self, fundamental: Dict) -> Tuple[float, Dict]:
        """
        计算价值面得分
        
        评分维度：
        - PE 合理性 (0-25分)
        - PB 合理性 (0-25分)
        - ROE 质量 (0-25分)
        - 成长性 (0-25分)
        
        Args:
            fundamental: 基本面数据
        
        Returns:
            (score, details) - 分数 0-1，详情
        """
        if not fundamental:
            return 0.5, {'available': False}
        
        score = 0
        details = {'available': True}
        
        # 1. PE 评分 (0-25分)
        pe = fundamental.get('pe', 0)
        if pe > 0:
            if pe < 15:
                pe_score = 25  # 低估值
            elif pe < 25:
                pe_score = 20  # 合理估值
            elif pe < 40:
                pe_score = 10  # 偏高
            elif pe < 60:
                pe_score = 5   # 高估值
            else:
                pe_score = 0   # 严重高估
        else:
            pe_score = 0  # 亏损
        score += pe_score
        details['pe'] = pe
        details['pe_score'] = pe_score
        
        # 2. PB 评分 (0-25分)
        pb = fundamental.get('pb', 0)
        if pb > 0:
            if pb < 1:
                pb_score = 25  # 破净
            elif pb < 2:
                pb_score = 20  # 低估
            elif pb < 4:
                pb_score = 15  # 合理
            elif pb < 6:
                pb_score = 5   # 偏高
            else:
                pb_score = 0   # 高估
        else:
            pb_score = 0
        score += pb_score
        details['pb'] = pb
        details['pb_score'] = pb_score
        
        # 3. ROE 评分 (0-25分)
        roe = fundamental.get('roe', 0)
        if roe > 20:
            roe_score = 25  # 优秀
        elif roe > 15:
            roe_score = 20  # 良好
        elif roe > 10:
            roe_score = 15  # 一般
        elif roe > 5:
            roe_score = 10  # 较差
        elif roe > 0:
            roe_score = 5   # 差
        else:
            roe_score = 0   # 亏损
        score += roe_score
        details['roe'] = roe
        details['roe_score'] = roe_score
        
        # 4. 成长性评分 (0-25分)
        profit_growth = fundamental.get('profit_growth', 0)
        revenue_growth = fundamental.get('revenue_growth', 0)
        
        growth_avg = (profit_growth + revenue_growth) / 2
        if growth_avg > 30:
            growth_score = 25  # 高成长
        elif growth_avg > 20:
            growth_score = 20  # 较高成长
        elif growth_avg > 10:
            growth_score = 15  # 稳定成长
        elif growth_avg > 0:
            growth_score = 10  # 低成长
        elif growth_avg > -10:
            growth_score = 5   # 轻微下滑
        else:
            growth_score = 0   # 大幅下滑
        score += growth_score
        details['profit_growth'] = profit_growth
        details['revenue_growth'] = revenue_growth
        details['growth_score'] = growth_score
        
        # 归一化到 0-1
        final_score = score / 100
        details['total_score'] = round(final_score, 3)
        
        return final_score, details


class TechnicalValidator:
    """技术面验证器
    
    从 MongoDB 获取行情数据，验证事件驱动股票的技术面质量
    """
    
    def __init__(self, db=None):
        self.db = db
        self._init_db()
    
    def _init_db(self):
        """初始化数据库连接"""
        if self.db is not None:
            return

        try:
            from factor_data_import_service import _load_mongodb_config, MongoFactorDataStore

            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config", "config_complete.yaml")
            mongodb_config = _load_mongodb_config(config_path)
            self._store = MongoFactorDataStore(mongodb_config)
            self.db = self._store.db
            self._collections = self._store.collections
            logger.info("✅ MongoDB 连接成功 (TechnicalValidator)")
        except Exception as e:
            logger.warning(f"⚠️ MongoDB 连接失败: {e}")
            self.db = None
            self._store = None

    def get_recent_quotes(self, code: str, days: int = 60) -> List[Dict]:
        """
        获取最近行情数据

        Args:
            code: 股票代码
            days: 天数

        Returns:
            行情数据列表（倒序，最新在前）
        """
        if self.db is None:
            return []

        try:
            # 尝试多种代码格式
            code_variants = [
                code,
                f"SH{code}" if code.startswith('6') else f"SZ{code}",
                f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
            ]

            for code_var in code_variants:
                quotes = list(self.db[self._collections['daily_quotes']].find(
                    {'code': code_var},
                    sort=[('trade_date', -1)],
                    limit=days
                ))
                if quotes:
                    return quotes
            
            return []
        except Exception as e:
            logger.debug(f"获取行情数据失败 ({code}): {e}")
            return []
    
    def calculate_technical_score(self, quotes: List[Dict]) -> Tuple[float, Dict]:
        """
        计算技术面得分
        
        评分维度：
        - 趋势强度 (0-25分): MA5 > MA10 > MA20
        - 动量 (0-25分): 近期涨幅
        - 成交量 (0-25分): 量能配合
        - 位置 (0-25分): 相对高低位
        
        Args:
            quotes: 行情数据
        
        Returns:
            (score, details) - 分数 0-1，详情
        """
        if not quotes or len(quotes) < 20:
            return 0.5, {'available': False, 'reason': '数据不足'}
        
        score = 0
        details = {'available': True}
        
        # 提取价格和成交量
        closes = [float(q.get('close', 0)) for q in quotes if q.get('close')]
        volumes = [float(q.get('volume', 0)) for q in quotes if q.get('volume')]
        
        if len(closes) < 20:
            return 0.5, {'available': False, 'reason': '价格数据不足'}
        
        current_price = closes[0]
        
        # 1. 趋势强度评分 (0-25分)
        ma5 = sum(closes[:5]) / 5
        ma10 = sum(closes[:10]) / 10
        ma20 = sum(closes[:20]) / 20
        
        trend_score = 0
        if current_price > ma5 > ma10 > ma20:
            trend_score = 25  # 强势多头排列
        elif current_price > ma5 > ma10:
            trend_score = 20  # 多头趋势
        elif current_price > ma5:
            trend_score = 15  # 短期向上
        elif current_price > ma20:
            trend_score = 10  # 中期支撑
        elif current_price < ma5 < ma10 < ma20:
            trend_score = 0   # 空头排列
        else:
            trend_score = 5   # 震荡
        
        score += trend_score
        details['ma5'] = round(ma5, 2)
        details['ma10'] = round(ma10, 2)
        details['ma20'] = round(ma20, 2)
        details['trend_score'] = trend_score
        
        # 2. 动量评分 (0-25分)
        if len(closes) >= 5 and closes[4] > 0:
            return_5d = (closes[0] - closes[4]) / closes[4] * 100
        else:
            return_5d = 0
        
        if len(closes) >= 20 and closes[19] > 0:
            return_20d = (closes[0] - closes[19]) / closes[19] * 100
        else:
            return_20d = 0
        
        # 动量评分：短期涨幅适中最佳（避免追高）
        if 3 <= return_5d <= 10:
            momentum_score = 25  # 温和上涨，最佳
        elif 0 < return_5d < 3:
            momentum_score = 20  # 小幅上涨
        elif 10 < return_5d <= 15:
            momentum_score = 15  # 涨幅较大，注意风险
        elif -3 <= return_5d <= 0:
            momentum_score = 10  # 小幅回调
        elif return_5d > 15:
            momentum_score = 5   # 涨幅过大，追高风险
        else:
            momentum_score = 0   # 大幅下跌
        
        score += momentum_score
        details['return_5d'] = round(return_5d, 2)
        details['return_20d'] = round(return_20d, 2)
        details['momentum_score'] = momentum_score
        
        # 3. 成交量评分 (0-25分)
        if volumes and len(volumes) >= 20:
            vol_5 = sum(volumes[:5]) / 5
            vol_20 = sum(volumes[:20]) / 20
            
            if vol_20 > 0:
                vol_ratio = vol_5 / vol_20
            else:
                vol_ratio = 1
            
            # 量能配合：温和放量最佳
            if 1.2 <= vol_ratio <= 2.0:
                volume_score = 25  # 温和放量
            elif 1.0 <= vol_ratio < 1.2:
                volume_score = 20  # 量能平稳
            elif 2.0 < vol_ratio <= 3.0:
                volume_score = 15  # 明显放量
            elif 0.7 <= vol_ratio < 1.0:
                volume_score = 10  # 轻微缩量
            elif vol_ratio > 3.0:
                volume_score = 5   # 异常放量
            else:
                volume_score = 0   # 严重缩量
        else:
            volume_score = 10
            vol_ratio = 1
        
        score += volume_score
        details['vol_ratio'] = round(vol_ratio, 2)
        details['volume_score'] = volume_score
        
        # 4. 位置评分 (0-25分)
        if len(closes) >= 60:
            high_60 = max(closes[:60])
            low_60 = min(closes[:60])
            
            if high_60 > low_60:
                position = (current_price - low_60) / (high_60 - low_60)
            else:
                position = 0.5
            
            # 位置评分：中低位最佳
            if 0.3 <= position <= 0.6:
                position_score = 25  # 中位，最佳
            elif 0.2 <= position < 0.3:
                position_score = 20  # 偏低位
            elif 0.6 < position <= 0.7:
                position_score = 15  # 偏高位
            elif position < 0.2:
                position_score = 10  # 低位（可能有问题）
            else:
                position_score = 5   # 高位风险
        else:
            position_score = 10
            position = 0.5
        
        score += position_score
        details['position'] = round(position, 2)
        details['position_score'] = position_score
        
        # 归一化到 0-1
        final_score = score / 100
        details['total_score'] = round(final_score, 3)
        
        return final_score, details


class EventDrivenStrategy:
    """事件驱动选股策略 v3.0
    
    双轨制的"事件轨"（真正的新闻驱动）：
    1. 从财联社快讯中提取股票代码（真正的新闻驱动）
    2. 从公司名称映射到股票代码
    3. 分析新闻情绪，统计每只股票的新闻数量
    4. 结合价值面、技术面进行多维度验证
    5. 负面情绪过滤 + ST股票过滤 + 样本数量验证
    6. 生成综合评分的事件驱动推荐
    
    评分权重：
    - 情绪分数: 30%
    - 价值面分数: 35%
    - 技术面分数: 35%
    
    v3.0 改进：
    - 真正的新闻驱动：从新闻内容中提取股票代码
    - 公司名称映射：支持从公司名称识别股票代码
    - 移除市场数据依赖（热榜/涨停板/龙虎榜）
    - 基于新闻数量和情绪进行排序
    """
    
    # 评分权重配置
    WEIGHT_SENTIMENT = 0.30
    WEIGHT_FUNDAMENTAL = 0.35
    WEIGHT_TECHNICAL = 0.35
    
    # 过滤阈值
    SENTIMENT_FILTER_THRESHOLD = -0.3  # 情绪低于此值则剔除
    MIN_NEWS_COUNT = 3  # 新闻数量低于此值则降低权重
    
    # ST 股票过滤（名称包含这些关键词则剔除）
    ST_KEYWORDS = ['ST', '*ST', 'S*ST', 'SST', 'S ST']
    
    # 名称黑名单（包含这些关键词则剔除）
    NAME_BLACKLIST = ['退市', '退', 'B股']
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.news_provider = NewsDataProvider()
        self.sentiment_analyzer = NewsSentimentAnalyzer()
        
        # 初始化价值面和技术面验证器
        self.fundamental_validator = FundamentalValidator()
        self.technical_validator = TechnicalValidator()
    
    def _is_st_stock(self, name: str) -> bool:
        """
        检查是否为 ST 股票
        
        Args:
            name: 股票名称
        
        Returns:
            是否为 ST 股票
        """
        if not name:
            return False
        
        name_upper = name.upper()
        for keyword in self.ST_KEYWORDS:
            if keyword in name_upper:
                return True
        return False
    
    def _is_blacklisted(self, name: str) -> bool:
        """
        检查是否在黑名单中
        
        Args:
            name: 股票名称
        
        Returns:
            是否在黑名单中
        """
        if not name:
            return False
        
        for keyword in self.NAME_BLACKLIST:
            if keyword in name:
                return True
        return False
    
    def get_event_driven_stocks(self, limit: int = 30,
                                 use_fundamental: bool = True,
                                 use_technical: bool = True) -> List[Dict]:
        """
        获取事件驱动股票（多维度综合评分）
        
        Args:
            limit: 返回数量限制
            use_fundamental: 是否使用价值面验证
            use_technical: 是否使用技术面验证
        
        Returns:
            事件驱动股票列表
        """
        logger.info("=" * 60)
        logger.info("事件驱动选股 v3.0 - 真正的新闻驱动 + 多维度综合评分")
        logger.info("=" * 60)
        logger.info(f"评分权重: 情绪 {self.WEIGHT_SENTIMENT:.0%} + 价值面 {self.WEIGHT_FUNDAMENTAL:.0%} + 技术面 {self.WEIGHT_TECHNICAL:.0%}")
        
        # 1. 获取热点股票
        hot_stocks = self.news_provider.get_hot_stocks_from_news()
        logger.info(f"获取热点股票: {len(hot_stocks)} 只")

        # 2. 分析每只热点股票（多维度）
        event_stocks = []
        filtered_count = 0
        st_filtered_count = 0
        low_news_count = 0
        
        for stock in hot_stocks[:limit * 2]:  # 多取一些，因为会过滤
            code = stock.get('code')
            name = stock.get('name', '')
            if not code:
                continue
            
            # ========== ST 股票过滤 ==========
            if self._is_st_stock(name):
                st_filtered_count += 1
                logger.debug(f"过滤 ST 股票: {code} {name}")
                continue
            
            # ========== 黑名单过滤 ==========
            if self._is_blacklisted(name):
                st_filtered_count += 1
                logger.debug(f"过滤黑名单股票: {code} {name}")
                continue
            
            # ========== 情绪分析 ==========
            stock_news = self.news_provider.get_stock_news(code, limit=10)

            news_texts = [(n.get('title', '') + ' ' + n.get('content', '')) for n in stock_news]
            sentiments = self.sentiment_analyzer.analyze_sentiment_batch(news_texts)
            sentiment_scores = [s['score'] for s in sentiments]

            themes = []
            for text in news_texts:
                themes.extend(self.sentiment_analyzer.extract_themes(text))
            
            avg_sentiment = sum(sentiment_scores) / len(sentiment_scores) if sentiment_scores else 0
            unique_themes = list(set(themes))
            news_count = len(stock_news)
            
            # ========== 负面情绪过滤 ==========
            if avg_sentiment < self.SENTIMENT_FILTER_THRESHOLD:
                filtered_count += 1
                logger.debug(f"过滤 {code}: 情绪过低 ({avg_sentiment:.2f})")
                continue
            
            # ========== 样本数量验证 ==========
            news_weight_factor = 1.0
            if news_count < self.MIN_NEWS_COUNT:
                news_weight_factor = 0.7  # 降低权重
                low_news_count += 1
                logger.debug(f"降权 {code}: 新闻数量不足 ({news_count})")
            
            # ========== 价值面验证 ==========
            fundamental_score = 0.5
            fundamental_details = {'available': False}
            
            if use_fundamental:
                fundamental_data = self.fundamental_validator.get_fundamental_data(code)
                fundamental_score, fundamental_details = self.fundamental_validator.calculate_value_score(fundamental_data)
            
            # ========== 技术面验证 ==========
            technical_score = 0.5
            technical_details = {'available': False}
            
            if use_technical:
                quotes = self.technical_validator.get_recent_quotes(code, days=60)
                technical_score, technical_details = self.technical_validator.calculate_technical_score(quotes)
            
            # ========== 综合评分 ==========
            # 情绪分数归一化到 0-1 (原始范围 -1 ~ +1)
            sentiment_normalized = (avg_sentiment + 1) / 2

            # 计算原始事件分数（用于来源加分等）
            event_base_score = self._calculate_event_score(stock, avg_sentiment, news_count, unique_themes)

            # 仅用有数据的维度，缺失维度的权重重新分配（与 pyramid 策略一致）
            dimension_scores = {
                'sentiment': sentiment_normalized * news_weight_factor,
                'fundamental': fundamental_score,
                'technical': technical_score,
            }
            dimension_available = {
                'sentiment': True,
                'fundamental': fundamental_details.get('available', False),
                'technical': technical_details.get('available', False),
            }
            dimension_weights = {
                'sentiment': self.WEIGHT_SENTIMENT,
                'fundamental': self.WEIGHT_FUNDAMENTAL,
                'technical': self.WEIGHT_TECHNICAL,
            }

            available_dims = [k for k, v in dimension_available.items() if v]
            if available_dims:
                avail_weight_sum = sum(dimension_weights[k] for k in available_dims)
                renorm_weights = {k: dimension_weights[k] / avail_weight_sum for k in available_dims}
                composite_score = sum(dimension_scores[k] * renorm_weights[k] for k in available_dims)
            else:
                composite_score = sentiment_normalized

            data_quality_score = round(len(available_dims) / len(dimension_available), 3)

            # 来源加分
            source_bonus = self._get_source_bonus(stock.get('source', ''))
            composite_score = min(1.0, composite_score + source_bonus)
            
            event_stocks.append({
                'code': code,
                'name': stock.get('name', ''),
                'source': stock.get('source', ''),
                'reason': stock.get('reason', ''),
                'news_count': news_count,
                'sentiment_score': round(avg_sentiment, 3),
                'sentiment_normalized': round(sentiment_normalized, 3),
                'themes': unique_themes,
                'event_score': round(event_base_score, 3),  # 保留原始事件分数
                'fundamental_score': round(fundamental_score, 3),
                'fundamental_details': fundamental_details,
                'technical_score': round(technical_score, 3),
                'technical_details': technical_details,
                'composite_score': round(composite_score, 3),  # 综合分数
                'data_quality_score': data_quality_score,
                'news_weight_factor': news_weight_factor,
                'track': 'event'
            })
        
        # 按综合分数排序
        event_stocks.sort(key=lambda x: x['composite_score'], reverse=True)
        
        logger.info(f"事件驱动选股完成:")
        logger.info(f"  - ST/黑名单过滤: {st_filtered_count} 只")
        logger.info(f"  - 负面情绪过滤: {filtered_count} 只")
        logger.info(f"  - 新闻不足降权: {low_news_count} 只")
        logger.info(f"  - 最终推荐: {min(len(event_stocks), limit)} 只")
        
        return event_stocks[:limit]
    
    def _get_source_bonus(self, source: str) -> float:
        """
        获取来源加分（v3.0 新闻驱动版本）
        
        Args:
            source: 股票来源
        
        Returns:
            加分值 (0-0.1)
        """
        # v3.0: 新闻提取的股票根据新闻数量加分，不再依赖市场数据来源
        source_bonuses = {
            '新闻提取': 0.05,  # 从新闻中提取的股票
            '财联社': 0.05,
            '东方财富': 0.04,
        }
        return source_bonuses.get(source, 0)
    
    def _calculate_event_score(self, stock: Dict, sentiment: float, news_count: int, themes: List[str]) -> float:
        """
        计算事件驱动基础分数（v3.0 新闻驱动版本）
        
        基于新闻事件因素计算分数：
        - 新闻数量（关注度）
        - 情绪分数
        - 热门主题
        
        Args:
            stock: 股票信息
            sentiment: 情绪分数
            news_count: 新闻数量
            themes: 主题列表
        
        Returns:
            事件驱动分数 (0-1)
        """
        score = 0.5  # 基础分
        
        # 1. 新闻数量加分（关注度）- v3.0 核心指标
        # 新闻越多，说明市场关注度越高
        if news_count >= 10:
            score += 0.20  # 高关注度
        elif news_count >= 5:
            score += 0.15  # 中等关注度
        elif news_count >= 3:
            score += 0.10  # 一般关注度
        elif news_count >= 2:
            score += 0.05  # 低关注度
        
        # 2. 情绪加分（负面情绪 < -0.3 已在调用方过滤，此处不再处理）
        if sentiment > 0.3:
            score += 0.15  # 强正面情绪
        elif sentiment > 0.1:
            score += 0.08  # 正面情绪
        elif sentiment < -0.1:
            score -= 0.08  # 负面情绪
        
        # 3. 热门主题加分
        hot_themes = ['人工智能', '半导体', '新能源', '数字经济', '军工', '医药医疗']
        theme_bonus = 0
        for theme in themes:
            if theme in hot_themes:
                theme_bonus += 0.05
        score += min(0.15, theme_bonus)  # 最多加0.15
        
        return min(1.0, max(0.0, score))
    
    def get_policy_driven_stocks(self) -> List[Dict]:
        """
        获取政策驱动股票
        
        分析政策新闻，提取受益行业和个股
        
        Returns:
            政策驱动股票列表
        """
        logger.info("分析政策驱动...")
        
        # 获取政策新闻
        policy_news = self.news_provider.get_policy_news(limit=30)
        
        # 统计主题出现频率
        theme_counts = defaultdict(int)
        theme_news = defaultdict(list)
        
        for news in policy_news:
            text = news.get('title', '') + ' ' + news.get('content', '')
            themes = self.sentiment_analyzer.extract_themes(text)
            
            for theme in themes:
                theme_counts[theme] += 1
                theme_news[theme].append(news.get('title', ''))
        
        # 按频率排序
        sorted_themes = sorted(theme_counts.items(), key=lambda x: x[1], reverse=True)
        
        policy_stocks = []
        for theme, count in sorted_themes[:5]:
            policy_stocks.append({
                'theme': theme,
                'news_count': count,
                'sample_news': theme_news[theme][:3],
                'track': 'policy'
            })
        
        logger.info(f"政策热点主题: {[t[0] for t in sorted_themes[:5]]}")
        
        return policy_stocks
    
    def run(self, use_fundamental: bool = True, use_technical: bool = True) -> Dict:
        """
        运行事件驱动策略 v3.0（真正的新闻驱动）
        
        Args:
            use_fundamental: 是否使用价值面验证
            use_technical: 是否使用技术面验证
        
        Returns:
            事件驱动报告
        """
        report_date = datetime.now().strftime('%Y-%m-%d')
        
        # 1. 获取事件驱动股票（多维度综合评分）
        event_stocks = self.get_event_driven_stocks(
            limit=30,
            use_fundamental=use_fundamental,
            use_technical=use_technical
        )
        
        # 2. 获取政策驱动主题
        policy_themes = self.get_policy_driven_stocks()
        
        # 3. 统计分析
        if event_stocks:
            avg_sentiment = sum(s['sentiment_score'] for s in event_stocks) / len(event_stocks)
            avg_composite = sum(s.get('composite_score', 0) for s in event_stocks) / len(event_stocks)

            # 只统计有数据的维度（排除 available=False 的默认值）
            fundamental_stocks = [s for s in event_stocks
                                  if s.get('fundamental_details', {}).get('available')]
            technical_stocks = [s for s in event_stocks
                                if s.get('technical_details', {}).get('available')]

            avg_fundamental = (sum(s['fundamental_score'] for s in fundamental_stocks) / len(fundamental_stocks)
                               if fundamental_stocks else 0)
            avg_technical = (sum(s['technical_score'] for s in technical_stocks) / len(technical_stocks)
                             if technical_stocks else 0)

            # 统计各维度优秀股票数量
            high_sentiment = sum(1 for s in event_stocks if s['sentiment_score'] > 0.3)
            high_fundamental = sum(1 for s in fundamental_stocks if s['fundamental_score'] > 0.6)
            high_technical = sum(1 for s in technical_stocks if s['technical_score'] > 0.6)
        else:
            avg_sentiment = avg_fundamental = avg_technical = avg_composite = 0
            high_sentiment = high_fundamental = high_technical = 0
        
        # 4. 生成报告
        report = {
            'metadata': {
                'system': '事件驱动选股系统（新闻驱动）',
                'version': '3.0.0',
                'report_date': report_date,
                'generated_at': datetime.now().isoformat(),
                'track': 'event',
                'weights': {
                    'sentiment': self.WEIGHT_SENTIMENT,
                    'fundamental': self.WEIGHT_FUNDAMENTAL,
                    'technical': self.WEIGHT_TECHNICAL
                },
                'filters': {
                    'sentiment_threshold': self.SENTIMENT_FILTER_THRESHOLD,
                    'min_news_count': self.MIN_NEWS_COUNT
                }
            },
            'event_stocks': event_stocks,
            'policy_themes': policy_themes,
            'summary': {
                'total_event_stocks': len(event_stocks),
                'top_themes': [t['theme'] for t in policy_themes[:3]],
                'avg_sentiment': round(avg_sentiment, 3),
                'avg_fundamental': round(avg_fundamental, 3),
                'avg_technical': round(avg_technical, 3),
                'avg_composite': round(avg_composite, 3),
                'high_sentiment_count': high_sentiment,
                'high_fundamental_count': high_fundamental,
                'high_technical_count': high_technical
            }
        }
        
        return report


class DualTrackMerger:
    """双轨合并器
    
    合并量化轨和事件轨的推荐结果
    """
    
    def __init__(self, quant_weight: float = 0.6, event_weight: float = 0.4):
        """
        初始化合并器
        
        Args:
            quant_weight: 量化轨权重
            event_weight: 事件轨权重
        """
        self.quant_weight = quant_weight
        self.event_weight = event_weight
    
    @staticmethod
    def normalize_code(code: str) -> str:
        """
        统一股票代码格式
        
        将各种格式的代码统一为6位数字格式：
        - SH600519 -> 600519
        - SZ000001 -> 000001
        - 600519.SH -> 600519
        - 600519 -> 600519
        
        Args:
            code: 原始股票代码
        
        Returns:
            6位数字格式的股票代码
        """
        if not code:
            return ''
        
        code = str(code).strip().upper()
        
        # 移除交易所前缀 (SH/SZ)
        if code.startswith('SH') or code.startswith('SZ'):
            code = code[2:]
        
        # 移除交易所后缀 (.SH/.SZ)
        if '.' in code:
            code = code.split('.')[0]
        
        # 确保是6位数字
        code = code.zfill(6)
        
        return code
    
    def merge(self, quant_stocks: List[Dict], event_stocks: List[Dict],
              top_n: int = 20) -> List[Dict]:
        """
        合并两轨推荐 (v2.0 - 支持多维度综合评分)
        
        策略：
        1. 两轨都推荐的股票 → 优先级最高
        2. 只有量化轨推荐 → 按量化分数排序
        3. 只有事件轨推荐 → 按综合分数排序（情绪+价值面+技术面）
        
        Args:
            quant_stocks: 量化轨股票列表
            event_stocks: 事件轨股票列表（v2.0 包含 composite_score）
            top_n: 返回数量
        
        Returns:
            合并后的推荐列表
        """
        # 建立代码索引（统一代码格式）
        quant_dict = {}
        for s in quant_stocks:
            normalized_code = self.normalize_code(s.get('code', ''))
            if normalized_code:
                quant_dict[normalized_code] = s
        
        event_dict = {}
        for s in event_stocks:
            normalized_code = self.normalize_code(s.get('code', ''))
            if normalized_code:
                event_dict[normalized_code] = s
        
        merged = []
        seen_codes = set()
        
        # 1. 找出两轨都推荐的股票（交集）
        common_codes = set(quant_dict.keys()) & set(event_dict.keys())
        
        for code in common_codes:
            quant_stock = quant_dict[code]
            event_stock = event_dict[code]
            
            # 合并分数
            quant_score = quant_stock.get('final_score', 0.5)
            # v2.0: 优先使用 composite_score（多维度综合分数）
            event_composite = event_stock.get('composite_score', event_stock.get('event_score', 0.5))
            
            merged_score = quant_score * self.quant_weight + event_composite * self.event_weight
            
            merged.append({
                **quant_stock,
                'event_score': event_stock.get('event_score', 0.5),
                'event_composite_score': event_composite,
                'event_sentiment': event_stock.get('sentiment_score', 0),
                'event_fundamental': event_stock.get('fundamental_score', 0.5),
                'event_technical': event_stock.get('technical_score', 0.5),
                'event_reason': event_stock.get('reason', ''),
                'event_themes': event_stock.get('themes', []),
                'merged_score': round(merged_score, 3),
                'track': 'both',  # 双轨推荐
                'priority': 1  # 最高优先级
            })
            seen_codes.add(code)
        
        # 2. 只有量化轨推荐的股票
        for code, stock in quant_dict.items():
            if code not in seen_codes:
                merged.append({
                    **stock,
                    'merged_score': round(stock.get('final_score', 0.5) * self.quant_weight, 3),
                    'track': 'quant',
                    'priority': 2
                })
                seen_codes.add(code)
        
        # 3. 只有事件轨推荐的股票
        for code, stock in event_dict.items():
            if code not in seen_codes:
                # v2.0: 使用 composite_score 而非 event_score
                event_composite = stock.get('composite_score', stock.get('event_score', 0.5))
                merged.append({
                    **stock,
                    'merged_score': round(event_composite * self.event_weight, 3),
                    'track': 'event',
                    'priority': 3
                })
                seen_codes.add(code)
        
        # 按优先级和分数排序
        # priority: 1=双轨(最高), 2=量化轨, 3=事件轨(最低)
        # 先按优先级升序（1最优先），再按分数降序
        merged.sort(key=lambda x: (x['priority'], -x['merged_score']))
        
        # 重新计算排名
        for i, stock in enumerate(merged[:top_n], 1):
            stock['rank'] = i
        
        return merged[:top_n]
    
    def generate_merged_report(self, quant_report: Dict, event_report: Dict) -> Dict:
        """
        生成合并报告
        
        Args:
            quant_report: 量化轨报告
            event_report: 事件轨报告
        
        Returns:
            合并报告
        """
        # 提取股票列表
        quant_stocks = quant_report.get('selected_stocks', [])
        event_stocks = event_report.get('event_stocks', [])
        
        # 合并
        merged_stocks = self.merge(quant_stocks, event_stocks, top_n=30)
        
        # 统计
        both_count = sum(1 for s in merged_stocks if s.get('track') == 'both')
        quant_only = sum(1 for s in merged_stocks if s.get('track') == 'quant')
        event_only = sum(1 for s in merged_stocks if s.get('track') == 'event')
        
        report = {
            'metadata': {
                'system': '双轨选股系统',
                'version': '1.0.0',
                'report_date': datetime.now().strftime('%Y-%m-%d'),
                'generated_at': datetime.now().isoformat(),
                'quant_weight': self.quant_weight,
                'event_weight': self.event_weight
            },
            'merged_stocks': merged_stocks,
            'quant_summary': quant_report.get('summary', {}),
            'event_summary': event_report.get('summary', {}),
            'merge_summary': {
                'total_recommendations': len(merged_stocks),
                'both_tracks': both_count,
                'quant_only': quant_only,
                'event_only': event_only,
                'policy_themes': event_report.get('policy_themes', [])
            }
        }
        
        return report


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='事件驱动选股策略')
    parser.add_argument('action', nargs='?', default='run',
                       choices=['run', 'hot', 'policy', 'news'])
    parser.add_argument('--code', '-c', type=str, help='股票代码')
    parser.add_argument('--limit', '-l', type=int, default=30, help='返回数量')
    
    args = parser.parse_args()
    
    print("=" * 80)
    print("📰 事件驱动选股系统 v3.0 (真正的新闻驱动)")
    print("=" * 80)
    
    strategy = EventDrivenStrategy()
    
    if args.action == 'hot':
        if args.action == 'hot':
            # 获取热点股票（从新闻中提取）
            hot_stocks = strategy.news_provider.get_hot_stocks_from_news()
            print(f"\n🔥 从新闻中提取的热点股票 ({len(hot_stocks)} 只):")
            print(f"   数据来源: 财联社快讯")
            print()
            for i, stock in enumerate(hot_stocks[:20], 1):
                sentiment_emoji = "📈" if stock.get('avg_sentiment', 0) > 0.1 else "📉" if stock.get('avg_sentiment', 0) < -0.1 else "➖"
                themes_str = ', '.join(stock.get('themes', [])[:2]) if stock.get('themes') else '-'
                print(f"{i}. {stock['code']} {stock.get('name', '')} {sentiment_emoji}")
                print(f"   新闻数: {stock.get('news_count', 0)} | 情绪: {stock.get('avg_sentiment', 0):.2f} | 主题: {themes_str}")
                print(f"   原因: {stock.get('reason', '')[:40]}...")
                print()
    elif args.action == 'policy':
        # 获取政策主题
        policy_themes = strategy.get_policy_driven_stocks()
        print(f"\n📋 政策热点主题:")
        for theme in policy_themes:
            print(f"\n  {theme['theme']} (新闻数: {theme['news_count']})")
            for news in theme['sample_news'][:2]:
                print(f"    - {news[:50]}...")
    
    elif args.action == 'news':
        # 获取个股新闻
        if not args.code:
            print("❌ 请指定股票代码 (--code)")
            return
        
        news_list = strategy.news_provider.get_stock_news(args.code, limit=10)
        print(f"\n📰 {args.code} 新闻 ({len(news_list)} 条):")
        for news in news_list:
            sentiment = strategy.sentiment_analyzer.analyze_sentiment(
                news.get('title', '') + ' ' + news.get('content', '')
            )
            emoji = "📈" if sentiment['sentiment'] == 'positive' else "📉" if sentiment['sentiment'] == 'negative' else "➖"
            print(f"\n  {emoji} {news['title']}")
            print(f"     来源: {news['source']} | 情绪: {sentiment['score']:.2f}")
    
    else:
        # 运行完整策略 v3.0
        report = strategy.run()
        
        print(f"\n📊 事件驱动推荐 Top 10 (新闻驱动 + 多维度综合评分):")
        print(f"   权重: 情绪 {strategy.WEIGHT_SENTIMENT:.0%} + 价值面 {strategy.WEIGHT_FUNDAMENTAL:.0%} + 技术面 {strategy.WEIGHT_TECHNICAL:.0%}")
        print()
        
        for i, stock in enumerate(report['event_stocks'][:10], 1):
            sentiment_emoji = "📈" if stock['sentiment_score'] > 0.1 else "📉" if stock['sentiment_score'] < -0.1 else "➖"
            themes_str = ', '.join(stock['themes'][:2]) if stock['themes'] else '-'
            
            # 显示各维度分数
            fundamental_score = stock.get('fundamental_score', 0.5)
            technical_score = stock.get('technical_score', 0.5)
            composite_score = stock.get('composite_score', 0.5)
            
            # 维度指示器
            f_indicator = "🟢" if fundamental_score > 0.6 else "🟡" if fundamental_score > 0.4 else "🔴"
            t_indicator = "🟢" if technical_score > 0.6 else "🟡" if technical_score > 0.4 else "🔴"
            
            print(f"{i}. {stock['code']} {stock['name']} - {sentiment_emoji} 综合:{composite_score:.2f}")
            print(f"   情绪:{stock['sentiment_score']:.2f} | 价值面:{f_indicator}{fundamental_score:.2f} | 技术面:{t_indicator}{technical_score:.2f}")
            print(f"   来源:{stock['source']} | 主题:{themes_str}")
            print()
        
        # 显示统计摘要
        summary = report.get('summary', {})
        print(f"📈 统计摘要:")
        print(f"   平均情绪: {summary.get('avg_sentiment', 0):.2f}")
        print(f"   平均价值面: {summary.get('avg_fundamental', 0):.2f}")
        print(f"   平均技术面: {summary.get('avg_technical', 0):.2f}")
        print(f"   高情绪股票: {summary.get('high_sentiment_count', 0)} 只")
        print(f"   高价值面股票: {summary.get('high_fundamental_count', 0)} 只")
        print(f"   高技术面股票: {summary.get('high_technical_count', 0)} 只")
        
        print(f"\n📋 政策热点主题:")
        for theme in report['policy_themes'][:3]:
            print(f"  - {theme['theme']} (新闻数: {theme['news_count']})")
        
        # 保存报告
        reports_dir = './reports'
        os.makedirs(reports_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filepath = os.path.join(reports_dir, f"event_driven_{timestamp}.json")
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        
        print(f"\n✅ 报告已保存: {filepath}")
        print(f"\n📖 图例: 🟢优秀(>0.6) 🟡一般(0.4-0.6) 🔴较差(<0.4)")


if __name__ == "__main__":
    main()
