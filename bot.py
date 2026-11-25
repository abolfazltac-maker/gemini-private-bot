def main() -> None:
    if not ALLOWED_USER_IDS:
        raise ValueError("ALLOWED_USER_IDS تنظیم نشده است.")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    init_db()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("facts", facts_cmd))
    app.add_handler(CommandHandler("forget", forget_cmd))
    app.add_handler(CommandHandler("draw", draw_cmd))
    app.add_handler(CommandHandler("sticker", sticker_cmd))
    app.add_handler(CommandHandler("gif", gif_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VOICE | filters.VIDEO_NOTE | filters.VIDEO,
            handle_media,
        )
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
