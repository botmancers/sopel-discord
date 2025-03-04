# coding=utf-8

from __future__ import (
    unicode_literals,
    absolute_import,
    division,
    print_function
)

import asyncio
import threading
import re

from sopel import module
from sopel import formatting
from sopel.config.types import (
    StaticSection, ValidatedAttribute, BaseValidated, NO_DEFAULT
)
from sopel.tools import (get_input, get_logger)

import discord

import requests
from requests.exceptions import HTTPError


DISCORD_API_VERSION = 6
DISCORD_API_URL = f'https://discord.com/api/v{DISCORD_API_VERSION}'

client = discord.Client()
logger = get_logger(__name__)

valid_message_pattern = r'^(?![!?]\s*\w+)'


@client.event
async def on_ready():
    logger.info(f"[discord] logged in as {client.user.name}, id: {client.user.id}")
    logger.debug(f"[discord] {client.user}")


@client.event
async def on_message(message):
    content = message.clean_content
    logger.debug(f"[discord] <{message.author.name}> {content}")
    logger.debug(f"[discord-msg] {message}")
    # the messages we relay are for some reason caught here, but we can
    # filter them because the author id for those messages will be the
    # same as the webhook id we're using to send (NOT our client.user.id)
    # of course hook id is a fucking string while message.author.id is an int
    hook = client.irc_bot.memory['webhooks'].get(message.channel.id, {})
    if hook and message.channel.id in client.channel_mappings \
            and message.author.id != int(hook['id']) \
            and re.match(valid_message_pattern, content):
        irc_channel = client.channel_mappings[message.channel.id]
        content = re.sub(r'<(:\w+:)\d+>', r'\1', content)
        if message.attachments:
            extra = []
            if content:
                extra.append(content)
            for attachment in message.attachments:
                extra.append(attachment.url)
            content = ' '.join(extra)
        content = content.replace('\n', ' ').strip()
        if content:
            if re.match(r'^_.+_$', content) and not message.attachments:
                # Discord uses markdown italics to denote /me action messages
                irc_message = '{} {}'.format(
                    message.author.name,
                    content[1:-1]
                )
                client.irc_bot.action(irc_message, irc_channel)
            else:
                name_fmt = formatting.color(formatting.bold(message.author.name), formatting.colors.LIGHT_CYAN)
                irc_message = '<{}> {}'.format(name_fmt, content)
                client.irc_bot.say(irc_message, irc_channel)


class DictAttribute(BaseValidated):
    '''Config attribute containing a list of key: value pairs.

    Key: value pairs are saved to the file as a comma-separated list.
    The spaces before and after each item are stripped.
    '''
    def __init__(self, name, default=None):
        default = default or {}
        super(DictAttribute, self).__init__(name, default=default)

    def parse(self, value):
        pairs = value.split(',')
        value = {}
        for item in pairs:
            k, v = item.split(':')
            value[k.strip()] = v.strip()
        return value

    def serialize(self, value):
        if not isinstance(value, dict):
            raise ValueError('DictAttribute value must be a dict')
        return ','.join(['{}:{}'.format(k, v) for k, v in value.items()])

    def configure(self, prompt, default, parent, section_name):
        each_prompt = '?'
        if isinstance(prompt, tuple):
            each_prompt = prompt[1]
            prompt = prompt[0]

            if default is not NO_DEFAULT:
                prompt = '{} [{}]'.format(prompt, default)
            else:
                default = ''
            values = []
            value = get_input(each_prompt + ' ') or default
            while value:
                values.append(value)
                value = get_input(each_prompt + ' ')
            return self.parse(','.join(values))


class DiscordSection(StaticSection):
    discord_token = ValidatedAttribute('discord_token')
    channel_mappings = DictAttribute('channel_mappings')


def _setup_webhooks(bot):
    bot.memory['webhooks'] = {}
    headers = {
        'Authorization': 'Bot {}'.format(bot.config.discord.discord_token)
    }
    for k, channel_id in bot.memory['channel_mappings'].items():
        try:
            r = requests.get(
                '{}/channels/{}/webhooks'.format(DISCORD_API_URL, channel_id),
                headers=headers
            )
            bot.memory['webhooks'][channel_id] = {}
            r.raise_for_status()
            for hook in r.json():
                if hook['name'] == 'discord-irc':
                    bot.memory['webhooks'][channel_id] = hook
            if not bot.memory['webhooks'][channel_id]:
                payload = {'name': 'discord-irc'}
                r = requests.post(
                    '{}/channels/{}/webhooks'.format(
                        DISCORD_API_URL,
                        channel_id
                    ),
                    headers=headers,
                    json=payload
                )
                r.raise_for_status()
                bot.memory['webhooks'][channel_id] = r.json()
        except HTTPError as e:
            print('Could not access webhook API for channel {}.'.format(
                channel_id))
            print('Make sure the bot user has the "Manage webhooks" permission'
                  'on the specified discord channel.')
            print(e)


def configure(config):
    config.define_section('discord', DiscordSection)
    config.discord.configure_setting(
        'discord_token',
        'Discord token for the app bot user'
    )
    config.discord.configure_setting(
        'channel_mappings',
        ('Comma-separated list of Discord channel ID to IRC channel mappings'
         ' (ex: 1234: #irc-channel1,'
         ' 5678: #irc-channel2)')
    )


def run_discord(loop):
    loop.run_forever()


def setup(bot):
    bot.config.define_section('discord', DiscordSection)
    client.irc_bot = bot
    client.channel_mappings = {
        int(k): v for k, v in bot.config.discord.channel_mappings.items()
    }
    print(client.channel_mappings)
    # config order maps discord: IRC, invert the map for the IRC bot
    bot.memory['channel_mappings'] = {
        v: k for k, v in client.channel_mappings.items()
    }
    _setup_webhooks(bot)
    # only start the asyncio thread once (the discord thread can survive sopel
    # restarts)
    loop = asyncio.get_event_loop()
    if not loop.is_running():
        loop.create_task(client.start(bot.config.discord.discord_token))
        t = threading.Thread(target=run_discord, args=(loop,))
        t.start()


# Match all messages except for those which start with common bot command
# prefixes
@module.require_chanmsg
@module.rule(valid_message_pattern)
def irc_message(bot, trigger):
    if not trigger.is_privmsg \
            and trigger.sender in bot.memory['channel_mappings']:
        discord_channel = bot.memory['channel_mappings'][trigger.sender]
        hook = bot.memory['webhooks'].get(discord_channel, {})
        if hook:
            headers = {
                'Authorization': 'Bot {}'.format(
                    bot.config.discord.discord_token)
            }
            content = trigger.match.string
            if trigger.tags.get('intent') == 'ACTION':
                content = '_{}_'.format(content)
            payload = {
                'content': content,
                'username': '{} (IRC)'.format(trigger.nick),
            }
            try:
                logger.debug(f"webhook id {hook['id']}")
                r = requests.post('{}/webhooks/{}/{}'.format(
                        DISCORD_API_URL, hook['id'], hook['token']
                    ),
                    headers=headers,
                    json=payload,
                )
                r.raise_for_status()
            except HTTPError as e:
                pass
