import os
import discord
from discord.ext import commands
import config
from cogs.library_desk import LibraryDeskView


class LibraryBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # cogsフォルダ内の .py ファイルをすべて自動で読み込む
        if os.path.exists("./cogs"):
            for filename in os.listdir("./cogs"):
                if filename.endswith(".py") and not filename.startswith("_"):
                    await self.load_extension(f"cogs.{filename[:-3]}")
                    print(f"Loaded Cog: {filename}")

        # 再起動後も受付カウンターのボタンが動くように永続Viewを登録
        self.add_view(LibraryDeskView())

        # スラッシュコマンドをDiscordに同期
        await self.tree.sync()

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")


bot = LibraryBot()

if __name__ == "__main__":
    bot.run(config.BOT_TOKEN)
