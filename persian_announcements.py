import logging
from telebot import types

logger = logging.getLogger("PersianAnnouncements")

# Library of Persian announcements tailored for the custom VPN configuration
VIP_ANNOUNCEMENTS = {
    1: {
        "title": "💎 رونمایی از سرورهای پرسرعت و اختصاصی VIP",
        "text": (
            "🚀 <b>کانفیگ‌های اختصاصی و سرورهای فوق‌العاده پایدار VIP فعال شدند!</b>\n\n"
            "در کنار کانفیگ‌های عمومی و رایگان کانال، برای دوستانی که به دنبال سرعت حداکثری، "
            "بدون قطعی و آی‌پی تمیز و اختصاصی هستند، سرویس ویژه ما آماده ارائه است:\n\n"
            "⚡ <b>ویژگی‌های منحصر‌به‌فرد سرورهای اختصاصی:</b>\n"
            "▫️ <b>ترافیک نامحدود در سرعت و پینگ فوق‌العاده</b> (مخصوص گیمینگ، وب‌گردی و استریم 4K)\n"
            "▫️ <b>اتصال دائمی و بدون اختلال</b> روی تمامی اپراتورها (همراه اول، ایرانسل، رایتل، مخابرات و وای‌فای)\n"
            "▫️ <b>تکنولوژی ضد فیلتر و فرگمنت پیشرفته</b> برای عبور از شدیدترین فیلترینگ‌های ملی\n"
            "▫️ <b>دسترسی بدون تحریم به هوش مصنوعی:</b> چت‌جی‌پی‌تی (ChatGPT)، جمینای (Gemini) و Claude بدون کپچا\n"
            "▫️ <b>مسیریابی هوشمند Iran-Safe:</b> سایت‌های بانکی و ایرانی بدون نیاز به قطع فیلترشکن مستقیم باز می‌شوند\n"
            "▫️ <b>لینک سابسکریپشن هوشمند:</b> آپدیت خودکار + نمایش دقیق حجم باقی‌مانده و روزشمار اعتبار در برنامه\n\n"
            "💳 <b>خرید آسان و مستقیم از طریق کریپتوکارنسی (USDT / TRX / ETH) در ربات:</b>\n"
            "برای مشاهده پلن‌ها و دریافت آنی، دکمه زیر را لمس کنید 👇"
        )
    },
    2: {
        "title": "🛡 اتصال صد در صد روی همراه اول، ایرانسل و رایتل",
        "text": (
            "📱 <b>پایان قطعی‌های اینترنت سیم‌کارت با تکنولوژی Direct IP + TLS Fragmentation!</b>\n\n"
            "حتماً متوجه شده‌اید که در ساعات اوج مصرف یا اینترنت دیتای موبایل (LTE/5G)، "
            "بسیاری از فیلترشکن‌ها قطع می‌شوند یا با افت شدید سرعت مواجهند.\n\n"
            "🔹 <b>چرا سرورهای اختصاصی ما همیشه متصل می‌مانند؟</b>\n"
            "ما با فعال‌سازی <b>تکه کردن پکت‌های TLS (Fragmentation)</b> و اتصال مستقیم از طریق آی‌پی اختصاصی، "
            "سیستم فیلترینگ اپراتورها را به طور کامل دور می‌زنیم. این یعنی حتی در شرایط اختلال شدید اینترنت ملی، "
            "ارتباط شما با بالاترین کیفیت و کمترین تاخیر برقرار می‌ماند.\n\n"
            "✨ سازگار با برنامه‌های v2rayNG، V2Box، Streisand، Nekoray، Clash و Sing-box.\n\n"
            "💎 برای دریافت پلن اختصاصی خود وارد ربات شوید 👇"
        )
    },
    3: {
        "title": "🤖 دسترسی تضمینی و بدون خطا به ChatGPT و هوش مصنوعی",
        "text": (
            "🧠 <b>دسترسی بدون فیلتر و بدون کپچا به تمامی ابزارهای هوش مصنوعی!</b>\n\n"
            "خیلی از فیلترشکن‌های رایگان به دلیل آی‌پی‌های اشتراکی یا کثیف، توسط OpenAI و گوگل مسدود شده "
            "و مدام خطای <code>Access Denied</code> یا کپچای طولانی می‌دهند.\n\n"
            "🌟 <b>در سرورهای اختصاصی ما:</b>\n"
            "▫️ خروجی مستقیم از سرورهای اختصاصی آمریکا و هلند\n"
            "▫️ آی‌پی کامپیوترها و سرورها کاملاً مسکونی (Clean Egress)\n"
            "▫️ لود سریع و روان ChatGPT، Claude، Perplexity، Midjourney و Google Gemini\n\n"
            "🚀 تجربه اینترنت آزاد و پرسرعت واقعی را در ربات تجربه کنید 👇"
        )
    },
    4: {
        "title": "🇮🇷 قابلیت Iran-Safe: دیگر نیازی به خاموش کردن VPN نیست!",
        "text": (
            "🔄 <b>ویژگی فوق‌العاده Iran-Safe در کانفیگ‌های اختصاصی ما</b>\n\n"
            "تا به حال شده وسط استفاده از اینترنت، برای کار با همراه بانک، اسنپ، تپسی یا دیجی‌کالا مجبور به قطع فیلترشکن شوید؟\n\n"
            "🛡 با معماری هوشمند <b>Iran-Safe</b>، کلیه سایت‌ها و اپلیکیشن‌های داخلی مستقیماً و بدون عبور از تانل باز می‌شوند:\n"
            "✅ بدون قطعی یا مسدود شدن حساب‌های بانکی\n"
            "✅ محاسبه اینترنت داخلی با تعرفه نیم‌بها\n"
            "✅ صرفه‌جویی چشمگیر در مصرف حجم فیلترشکن شما\n\n"
            "⚡ اشتراک اختصاصی خود را با تحویل آنی از ربات تهیه کنید 👇"
        )
    },
    5: {
        "title": "💖 حمایت مالی و دونیت برای پایداری کانال",
        "text": (
            "🌟 <b>همراهان گرامی کانال!</b>\n\n"
            "تیم ما به صورت شبانه‌روزی در حال اسکن، تست سلامت و ارائه صدها کانفیگ رایگان در کانال است. "
            "نگهداری سرورهای اسکنر و پهنای باند هزینه‌های دلاری سنگینی به همراه دارد.\n\n"
            "اگر کانفیگ‌های رایگان ما برای شما مفید بوده و تمایل به حمایت مالی از این پروژه آزاد دارید، "
            "می‌توانید کمک‌های ارزشمند خود را از طریق آدرس‌های رمزارز زیر واریز فرمایید:\n\n"
            "🔺 <b>شبکه ترون (TRX / USDT-TRC20):</b>\n"
            "<code>TLydCCA4FCSPczXXmDDrmhJQsK9ePXL8sQ</code>\n\n"
            "🔹 <b>شبکه اتریوم (ETH / USDT-ERC20):</b>\n"
            "<code>0x225f3f2113B5C81A907dFeFA1551e88239cBF2EA</code>\n\n"
            "🙏 حمایت‌های شما انگیزه و توان ما را برای ارائه سرویس رایگان و پرسرعت دوچندان می‌کند!"
        )
    }
}


def build_channel_vip_markup(bot_username: str):
    """Inline button markup linking to the bot for purchasing VIP configs."""
    markup = types.InlineKeyboardMarkup(row_width=1)
    if bot_username:
        markup.add(
            types.InlineKeyboardButton("💎 خرید و دریافت کانفیگ اختصاصی (VIP)", url=f"https://t.me/{bot_username}?start=buy"),
            types.InlineKeyboardButton("🤖 ورود به ربات و دسترسی کامل", url=f"https://t.me/{bot_username}")
        )
    return markup


def send_persian_announcement(bot, channel_id: str, bot_username: str, template_id: int = 1) -> bool:
    """Send a selected Persian announcement to the Telegram channel."""
    if not channel_id:
        logger.warning("CHANNEL_ID is not configured, skipping announcement post")
        return False

    tmpl = VIP_ANNOUNCEMENTS.get(template_id, VIP_ANNOUNCEMENTS[1])
    text = tmpl["text"]
    markup = build_channel_vip_markup(bot_username)

    try:
        bot.send_message(
            channel_id,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True
        )
        logger.info("Successfully posted Persian announcement #%d ('%s') to %s", template_id, tmpl['title'], channel_id)
        return True
    except Exception as e:
        logger.error("Failed to post Persian announcement to %s: %s", channel_id, e)
        return False
