import random
import logging
import subprocess
import sys
import os
import re
import time
import discord
from discord.ext import commands, tasks
import docker
import asyncio
from discord import app_commands
import sqlite3
from dotenv import load_dotenv
from datetime import datetime, timezone

# Load environment variables
load_dotenv()

# Configuration from .env
TOKEN = os.getenv('TOKEN', 'MTUzNDI4NDA1NzUwMjgxMDE5Mg.GKS9bV.tNaKRzFHKbc6jzXvyZeoRvjbSwSv8QfFJNnMdI')
ADMIN_ID = int(os.getenv('ADMIN_ID', 1353572110592643076))  # Admin user ID for checks
BOT_STATUS_NAME = os.getenv('BOT_STATUS_NAME', 'KingCloud')
WATERMARK = os.getenv('WATERMARK', 'Powered by KingCloud VPS Bot')
# VPS Defaults from .env
DEFAULT_RAM = os.getenv('DEFAULT_RAM', '6g')  # e.g., '2g', '4G'
DEFAULT_CPU = os.getenv('DEFAULT_CPU', '1')  # Lowered default to '1' to avoid common errors
DEFAULT_DISK = os.getenv('DEFAULT_DISK', '20G')  # e.g., '20G' - Note: Disk limit not enforced in container
VPS_HOSTNAME = os.getenv('VPS_HOSTNAME', 'kingcloud-free')  # Base hostname, append user ID
SERVER_LIMIT = int(os.getenv('SERVER_LIMIT', 1))
TOTAL_SERVER_LIMIT = int(os.getenv('TOTAL_SERVER_LIMIT', 17))  # Global total running server limit
DATABASE_FILE = os.getenv('DATABASE_FILE', 'vps_bot.db')
DEPLOY_COOLDOWN_SECONDS = 1800
free_vps_cooldowns = {}

def get_deploy_cooldown(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS deploy_cooldowns (
            user_id INTEGER PRIMARY KEY,
            last_deploy REAL NOT NULL
        )
    """)
    conn.commit()
    cursor.execute(
        "SELECT last_deploy FROM deploy_cooldowns WHERE user_id = ?",
        (user_id,)
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return 0.0
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return 0.0

def set_deploy_cooldown(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS deploy_cooldowns (
            user_id INTEGER PRIMARY KEY,
            last_deploy REAL NOT NULL
        )
    """)
    cursor.execute("""
        INSERT INTO deploy_cooldowns (user_id, last_deploy)
        VALUES (?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET last_deploy = excluded.last_deploy
    """, (user_id, time.time()))
    conn.commit()
    conn.close()


# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('vps_bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Intents
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)
client = docker.from_env()

# Persistent additional admin users
def init_admin_users():
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_users (
            user_id INTEGER PRIMARY KEY
        )
    """)
    conn.commit()
    conn.close()

def is_extra_admin(user_id):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT 1 FROM admin_users WHERE user_id = ?",
        (int(user_id),)
    ).fetchone()
    conn.close()
    return row is not None


def is_admin(member):
    user_id = getattr(member, "id", None)
    if user_id is None:
        return False

    # Primary admin from .env + persistent additional admins.
    return int(user_id) == ADMIN_ID or is_extra_admin(user_id)


def safe_fromisoformat(value):
    if not value:
        return None
    try:
        value = str(value)
        # Python supports microseconds (6 digits), not nanoseconds.
        value = re.sub(
            r'(\.\d{6})\d+',
            r'\1',
            value
        )
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception as e:
        logger.error(f"Invalid timestamp {value!r}: {e}")
        return None

# Database setup with SQLite3
def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    default_ram = DEFAULT_RAM
    default_cpu = DEFAULT_CPU
    default_disk = DEFAULT_DISK
    sql = f'''
        CREATE TABLE IF NOT EXISTS vps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            container_id TEXT UNIQUE NOT NULL,
            container_name TEXT NOT NULL,
            os_type TEXT NOT NULL,
            hostname TEXT NOT NULL,
            status TEXT DEFAULT 'stopped',
            ssh_command TEXT,
            ram TEXT DEFAULT '{default_ram}',
            cpu TEXT DEFAULT '{default_cpu}',
            disk TEXT DEFAULT '{default_disk}',
            suspended INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    '''
    cursor.execute(sql)
    cursor.execute("PRAGMA table_info(vps)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'suspended' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN suspended INTEGER DEFAULT 0")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_vps_audit_logs():
    conn = get_db_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vps_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                container_id TEXT,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
    finally:
        conn.close()


def log_vps_event(user_id, container_id, action, details=""):
    try:
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO vps_audit_logs
            (user_id, container_id, action, details, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(user_id),
                str(container_id) if container_id else None,
                str(action),
                str(details)[:1000],
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            )
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Failed to write VPS audit log")


def kc_status(vps):
    """Return a safe human-readable VPS status."""
    try:
        container_id = vps["container_id"]
        container = client.containers.get(container_id)
        status = str(container.status).lower()
        return {
            "running": "🟢 Running",
            "stopped": "🔴 Stopped",
            "paused": "⏸️ Paused",
            "restarting": "🔄 Restarting",
            "created": "🟡 Created",
            "exited": "🔴 Stopped",
            "dead": "⚫ Dead"
        }.get(status, f"⚪ {status.title()}")
    except Exception:
        status = str(vps.get("status", "unknown")).lower()
        return {
            "running": "🟢 Running",
            "stopped": "🔴 Stopped",
            "paused": "⏸️ Paused",
            "exited": "🔴 Stopped"
        }.get(status, f"⚪ {status.title()}")

def kc_os_name(os_type):
    """Return a safe display name for the VPS operating system."""
    value = str(os_type or "").strip().lower()
    if value in ("ubuntu", "ubuntu:22.04", "ubuntu22", "ubuntu-22.04"):
        return "Ubuntu 22.04"
    if value in ("debian", "debian:bookworm", "debian12", "debian-12"):
        return "Debian 12"
    return str(os_type or "Unknown OS").strip() or "Unknown OS"

def kc_embed(title=None, description=None, color=None, timestamp=None, **kwargs):
    """Safe KingCloud embed helper."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=color if color is not None else discord.Color.blurple(),
        timestamp=timestamp
    )
    if bot.user:
        avatar = bot.user.display_avatar.url
        embed.set_footer(
            text="KingCloud Free VPS Manager • Secure • Fast • Reliable",
            icon_url=avatar
        )
    return embed


init_vps_audit_logs()

def get_vps_audit_logs(user_id, container_id=None, limit=25):
    conn = get_db_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vps_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                container_id TEXT,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()

        if container_id:
            rows = conn.execute("""
                SELECT user_id, container_id, action, details, created_at
                FROM vps_audit_logs
                WHERE user_id = ? AND container_id = ?
                ORDER BY id DESC
                LIMIT ?
            """, (user_id, container_id, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT user_id, container_id, action, details, created_at
                FROM vps_audit_logs
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
            """, (user_id, limit)).fetchall()

        return rows
    finally:
        conn.close()


def add_user(user_id, username):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    conn.close()

def add_ban(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO bans (user_id) VALUES (?)', (user_id,))
    conn.commit()
    conn.close()

def remove_ban(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM bans WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM bans WHERE user_id = ?', (user_id,))
    banned = cursor.fetchone() is not None
    conn.close()
    return banned

def add_vps(user_id, container_id, container_name, os_type, hostname, ssh_command, ram=DEFAULT_RAM, cpu=DEFAULT_CPU, disk=DEFAULT_DISK):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO vps (user_id, container_id, container_name, os_type, hostname, status, ssh_command, ram, cpu, disk, suspended)
        VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, 0)
    ''', (user_id, container_id, container_name, os_type, hostname, ssh_command, ram, cpu, disk))
    conn.commit()
    conn.close()

def get_user_vps(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
    vps_list = cursor.fetchall()
    conn.close()
    return vps_list

def count_user_vps(user_id):
    return len(get_user_vps(user_id))

def get_vps_by_container_id(container_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE container_id = ?', (container_id,))
    vps = cursor.fetchone()
    conn.close()
    return vps

def get_vps_by_identifier(user_id, identifier):
    vps_list = get_user_vps(user_id)
    if not identifier:
        return vps_list[0] if vps_list else None
    identifier_lower = identifier.lower()
    for vps in vps_list:
        if (identifier_lower in vps['container_id'].lower() or
            identifier_lower in vps['container_name'].lower()):
            return vps
    return None

def update_vps_status(container_id, status):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET status = ? WHERE container_id = ?', (status, container_id))
    conn.commit()
    conn.close()

def update_vps_ssh(container_id, ssh_command):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET ssh_command = ? WHERE container_id = ?', (ssh_command, container_id))
    conn.commit()
    conn.close()

def update_vps_suspended(container_id, suspended):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE vps SET suspended = ? WHERE container_id = ?', (suspended, container_id))
    conn.commit()
    conn.close()

def delete_vps(container_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM vps WHERE container_id = ?', (container_id,))
    conn.commit()
    conn.close()

def get_total_instances():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM vps WHERE status = "running"')
    count = cursor.fetchone()[0]
    conn.close()
    return count

def parse_gb(resource_str):
    match = re.match(r'(\d+(?:\.\d+)?)([mMgG])?', resource_str.lower())
    if match:
        num = float(match.group(1))
        unit = match.group(2) or 'g'
        if unit in ['g', '']:
            return num
        elif unit in ['m']:
            return num / 1024.0
    return 0.0

def get_uptime(container_id):
    try:
        output = subprocess.check_output(["docker", "inspect", "-f", "{{.State.StartedAt}}", container_id], stderr=subprocess.STDOUT).decode().strip()
        if output == "<no value>":
            return "Not running"
        start_time = datetime.fromisoformat(output.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)
        uptime = now - start_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m"
    except Exception as e:
        logger.error(f"Uptime error for {container_id}: {e}")
        return "Unknown"

def get_stats(container_id):
    try:
        output = subprocess.check_output([
            "docker", "stats", "--no-stream", "--format",
            "{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}",
            container_id
        ], stderr=subprocess.STDOUT).decode().strip()
        parts = output.split('\t')
        if len(parts) == 3:
            cpu, mem, net = parts
            return {'cpu': cpu, 'mem': mem, 'net': net}
    except Exception as e:
        logger.error(f"Stats error for {container_id}: {e}")
    return {'cpu': 'N/A', 'mem': 'N/A', 'net': 'N/A'}

def get_logs(container_id, lines=50):
    try:
        output = subprocess.check_output(["docker", "logs", "--tail", str(lines), container_id], stderr=subprocess.STDOUT).decode()
        return output[-2000:]  # Truncate for Discord limit
    except Exception as e:
        logger.error(f"Logs error for {container_id}: {e}")
        return "Failed to fetch logs"

# Async Docker helpers
async def async_docker_run(image, hostname, ram, cpu, disk, container_name):
    cmd = [
        "docker", "run", "-d",
        "--privileged", "--cap-add=ALL",
        "--restart", "unless-stopped",
        f"--memory={ram}",
        f"--cpus={cpu}",
        f"--hostname={hostname}",
        f"--name={container_name}",
        image,
        "tail", "-f", "/dev/null"
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        if proc.returncode != 0:
            logger.error(f"Docker run failed: {stderr.decode()}")
            return None
        return stdout.decode().strip()
    except asyncio.TimeoutError:
        logger.error("Docker run timed out")
        return None
    except Exception as e:
        logger.error(f"Docker run error: {e}")
        return None

async def async_docker_start(container_id):
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "start", container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(), timeout=30.0)
        return proc.returncode == 0
    except asyncio.TimeoutError:
        logger.warning(f"Docker start timeout for {container_id}")
        return False
    except Exception as e:
        logger.error(f"Docker start error for {container_id}: {e}")
        return False

async def async_docker_stop(container_id):
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stop", container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(), timeout=30.0)
        return proc.returncode == 0
    except asyncio.TimeoutError:
        logger.warning(f"Docker stop timeout for {container_id}")
        try:
            await asyncio.create_subprocess_exec("docker", "kill", container_id, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL).communicate()
        except:
            pass
        return False
    except Exception as e:
        logger.error(f"Docker stop error for {container_id}: {e}")
        return False

async def async_docker_restart(container_id):
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "restart", container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await asyncio.wait_for(proc.communicate(), timeout=30.0)
        return proc.returncode == 0
    except asyncio.TimeoutError:
        logger.warning(f"Docker restart timeout for {container_id}")
        return False
    except Exception as e:
        logger.error(f"Docker restart error for {container_id}: {e}")
        return False

async def async_docker_rm(container_id):
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.communicate()
        return proc.returncode == 0
    except Exception as e:
        logger.error(f"Docker rm error for {container_id}: {e}")
        return False

async def async_install_tmate(container_id, os_type):
    install_cmd = "apt-get update && apt-get install -y tmate curl wget sudo openssh-client"
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "bash", "-c", install_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=600.0)
        if proc.returncode != 0:
            logger.warning(f"Tmate install warning for {container_id}: {stderr.decode()}")
        else:
            logger.info(f"Tmate installed in {container_id}")
    except asyncio.TimeoutError:
        logger.error(f"Tmate install timeout for {container_id}")
    except Exception as e:
        logger.error(f"Failed to install tmate in {container_id}: {e}")

# SSH capture
async def capture_ssh_session_line(process):
    while True:
        try:
            output = await asyncio.wait_for(process.stdout.readline(), timeout=30.0)
            if not output:
                break
            output = output.decode('utf-8').strip()
            if "ssh session:" in output.lower():
                return output.split("ssh session:")[-1].strip()
        except asyncio.TimeoutError:
            break
    return None

async def docker_exec_tmate(container_id):
    try:
        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, "tmate", "-F",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        return exec_cmd
    except Exception as e:
        logger.error(f"Tmate exec failed: {e}")
        return None

# Generic regen SSH
async def regen_ssh_command(interaction: discord.Interaction, vps_identifier, send_response=True, target_user=None):
    if target_user is None:
        target_user = interaction.user
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="No active VPS found.", color=discord.Color.red())
        if send_response:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    if vps['status'] != "running":
        embed = discord.Embed(description="VPS must be running to generate SSH.", color=discord.Color.red())
        if send_response:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        return False
    if send_response:
        await interaction.response.defer(ephemeral=True)
    container_id = vps['container_id']
    exec_process = await docker_exec_tmate(container_id)
    if exec_process:
        ssh_line = await capture_ssh_session_line(exec_process)
        if ssh_line:
            update_vps_ssh(container_id, ssh_line)
            embed = discord.Embed(title="🔐 New SSH Session Generated", description=f"✅ **Your SSH session is ready!**\n\n**SSH Command:**\n`{ssh_line}`\n\n⏳ **Expires:** 1 hour\n⚠️ **Do not share this command with anyone.**", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
            embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
            try:
                await target_user.send(embed=embed)
            except discord.Forbidden:
                logger.warning(f"Cannot DM user {target_user.id}")
                if send_response:
                    embed_dm_fail = discord.Embed(description="New SSH session generated but could not send to DMs (privacy settings).", color=discord.Color.orange())
                    await interaction.followup.send(embed=embed_dm_fail, ephemeral=True)
                else:
                    return True
            if send_response:
                embed_success = discord.Embed(description="New SSH session sent to your DMs.", color=discord.Color.green())
                await interaction.followup.send(embed=embed_success, ephemeral=True)
            return True
        else:
            embed = discord.Embed(description="Failed to generate SSH session.", color=discord.Color.red())
            if send_response:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return False
    else:
        embed = discord.Embed(description="Failed to execute tmate.", color=discord.Color.red())
        if send_response:
            await interaction.followup.send(embed=embed, ephemeral=True)
        return False

# Start/Stop/Restart helpers
async def manage_vps(interaction: discord.Interaction, vps_identifier, action, target_user=None):
    if target_user is None:
        target_user = interaction.user
    await interaction.response.defer(ephemeral=True)
    vps = get_vps_by_identifier(target_user.id, vps_identifier)

    log_vps_event(
        interaction.user.id,
        vps["container_id"],
        f"VPS ACTION: {action.upper()}",
        f"VPS: {vps['container_name']}"
    )

    if not vps:
        embed = discord.Embed(description="No VPS found.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    log_vps_event(
        interaction.user.id,
        vps["container_id"],
        f"VPS ACTION: {action.upper()}",
        f"VPS: {vps['container_name']}"
    )

    if action == "start" and vps['suspended'] and target_user == interaction.user:
        embed = discord.Embed(description="This VPS is suspended by an admin. Contact support.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    container_id = vps['container_id']
    os_type = vps['os_type']
    success = False
    if action == "start":
        success = await async_docker_start(container_id)
        if success:
            update_vps_status(container_id, "running")
    elif action == "stop":
        success = await async_docker_stop(container_id)
        if success:
            update_vps_status(container_id, "stopped")
    elif action == "restart":
        success = await async_docker_restart(container_id)
        if success:
            update_vps_status(container_id, "running")
    if success:
        os_name = "Ubuntu 22.04" if os_type == "ubuntu" else "Debian 12"
        embed = discord.Embed(title=f"VPS {action.title()}ed Successfully", description=f"OS: {os_name}", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
        if action in ["start", "restart"]:
            regen_success = await regen_ssh_command(interaction, vps_identifier, send_response=False, target_user=target_user)
            if regen_success:
                embed.description += "\nNew SSH session sent to DMs."
            else:
                embed.description += "\nFailed to generate new SSH session."
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        embed = discord.Embed(description=f"Failed to {action} the VPS.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)

# Reinstall helper
async def reinstall_vps(interaction: discord.Interaction, vps_identifier, os_type, target_user=None):
    if target_user is None:
        target_user = interaction.user
    await interaction.response.defer(ephemeral=True)
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="No VPS found.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return
    container_id = vps['container_id']
    user_id = vps['user_id']
    hostname = vps['hostname']
    ram, cpu, disk = vps['ram'], vps['cpu'], vps['disk']
    # Stop and remove
    await async_docker_stop(container_id)
    await asyncio.sleep(2)
    await async_docker_rm(container_id)
    delete_vps(container_id)
    # Create new with unique name
    suffix = random.randint(1000, 9999)
    new_container_name = f"{os_type}-vps-{user_id}-{suffix}"
    image = "ubuntu:22.04" if os_type == "ubuntu" else "debian:bookworm"
    new_container_id = await async_docker_run(image, hostname, ram, cpu, disk, new_container_name)
    if new_container_id:
        await async_install_tmate(new_container_id, os_type)
        await asyncio.sleep(10)  # Wait longer for install
        exec_process = await docker_exec_tmate(new_container_id)
        ssh_line = await capture_ssh_session_line(exec_process)
        if ssh_line:
            add_vps(user_id, new_container_id, new_container_name, os_type, hostname, ssh_line, ram, cpu, disk)
            os_name = "Ubuntu 22.04" if os_type == "ubuntu" else "Debian 12"
            embed = discord.Embed(title="VPS Reinstalled Successfully", description=f"OS: {os_name}\n```{ssh_line}```", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
            embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
            try:
                await target_user.send(embed=embed)
            except discord.Forbidden:
                logger.warning(f"Cannot DM user {target_user.id} for reinstall")
            embed_success = discord.Embed(description="VPS has been reinstalled. Check your DMs for details.", color=discord.Color.green())
            await interaction.followup.send(embed=embed_success, ephemeral=True)
        else:
            embed = discord.Embed(description="Reinstall failed: Unable to generate SSH.", color=discord.Color.red())
            await interaction.followup.send(embed=embed, ephemeral=True)
            await async_docker_rm(new_container_id)
    else:
        embed = discord.Embed(description="Reinstall failed: Docker creation error.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)

# Create VPS helper
async def create_vps(
    interaction,
    os_type,
    ram=DEFAULT_RAM,
    cpu=DEFAULT_CPU,
    disk=DEFAULT_DISK,
    target_user=None
):
    # HARD GLOBAL SLOT LIMIT
    current_slots = get_total_instances()
    if current_slots >= TOTAL_SERVER_LIMIT:
        embed = kc_embed(
            "⚠️  NO FREE SLOTS",
            f"All **{TOTAL_SERVER_LIMIT}** VPS slots are currently allocated.",
            discord.Color.orange()
        )
        if not interaction.response.is_done():
            await interaction.response.send_message(
                embed=embed,
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                embed=embed,
                ephemeral=True
            )
        return


    if target_user is None:
        target_user = interaction.user

    user_id = target_user.id
    username = str(target_user)

    add_user(user_id, username)
    
    # 30-minute cooldown for EVERY deployment, including admins
    import time
    now = time.time()
    last_deploy = get_deploy_cooldown(user_id)
    remaining = DEPLOY_COOLDOWN_SECONDS - (now - last_deploy)

    if remaining > 0:
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)

        embed = discord.Embed(
            title="⏳ Deployment Cooldown",
            description=(
                "You recently created a Free VPS.\n\n"
                f"Please wait **{minutes}m {seconds}s** before creating another one."
            ),
            color=discord.Color.orange()
        )
        embed.add_field(
            name="Cooldown",
            value="🕒 **30 Minutes**",
            inline=True
        )
        embed.add_field(
            name="Try Again",
            value=f"⏱️ **{minutes}m {seconds}s**",
            inline=True
        )
        embed.set_footer(text="KingCloud • Free VPS")

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )
        return


    # ─────────────────────────────────────────────
    # USER CHECKS
    # ─────────────────────────────────────────────

    if is_banned(user_id):
        embed = discord.Embed(
            title="🚫 Deployment Unavailable",
            description="You are currently banned from creating VPS instances.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )
        return

    current_vps = count_user_vps(user_id)

    if current_vps >= SERVER_LIMIT:
        embed = discord.Embed(
            title="⚠️ VPS Create Limit Reached",
            description=(
                f"You already have **{current_vps}/{SERVER_LIMIT} Free VPS**.\n\n"
                "Remove your current VPS before creating another one."
            ),
            color=discord.Color.orange()
        )
        embed.add_field(
            name="Your VPS",
            value=f"📦 **{current_vps}/{SERVER_LIMIT}**",
            inline=True
        )
        embed.add_field(
            name="Next Step",
            value="🗑️ Remove a VPS",
            inline=True
        )
        embed.set_footer(text="KingCloud • Free VPS")

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )
        return

    # ─────────────────────────────────────────────
    # RESOURCE VALIDATION
    # ─────────────────────────────────────────────

    try:
        host_info = client.info()

        host_cpus = host_info["NCPU"]
        host_mem_gb = host_info["MemTotal"] / (1024 ** 3)

        req_cpu = float(cpu)
        req_ram = parse_gb(ram)

        if req_cpu > host_cpus:
            embed = discord.Embed(
                title="⚠️ Resource Limit",
                description=(
                    f"Requested CPU: **{req_cpu} vCPU**\n"
                    f"Host CPU available: **{host_cpus} vCPU**"
                ),
                color=discord.Color.red()
            )
            await interaction.response.send_message(
                embed=embed,
                ephemeral=True
            )
            return

        if req_ram > host_mem_gb:
            embed = discord.Embed(
                title="⚠️ Resource Limit",
                description=(
                    f"Requested RAM: **{req_ram:.1f} GB**\n"
                    f"Host RAM available: **{host_mem_gb:.1f} GB**"
                ),
                color=discord.Color.red()
            )
            await interaction.response.send_message(
                embed=embed,
                ephemeral=True
            )
            return

    except Exception as e:
        logger.error(f"Resource validation failed: {e}")

        embed = discord.Embed(
            title="❌ Deployment Error",
            description="Unable to validate VPS resources. Please contact an administrator.",
            color=discord.Color.red()
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )
        return

    # ─────────────────────────────────────────────
    # DEPLOYING UI
    # ─────────────────────────────────────────────

    # Lock deployment immediately to prevent double-click / concurrent deployments


    # Lock cooldown immediately when deployment starts.
    # Applies to everyone, including admins, and prevents double-clicks.

    # 30-minute deployment lock
    # Applies to everyone, including admins.
    # Persistent 30-minute deployment lock
    # Applies to everyone, including admins.

    # Lock immediately when Deploy VPS is pressed.
    # Persistent SQLite cooldown; applies to admins too.

    # Permanent 30-minute lock for this user
    set_deploy_cooldown(user_id)

    await interaction.response.defer(ephemeral=True)

    progress_embed = discord.Embed(
        title="🚀 Deploying Your Free VPS",
        description=(
            "Your **KingCloud Free VPS** is being prepared.\n\n"
            "⏳ **Please wait...**\n\n"
            "🔧 Preparing VPS environment\n"
            "📦 Setting up container\n"
            "⚙️ Applying resources\n"
            "🔐 Preparing secure SSH access\n\n"
            "☁️ **KingCloud Infrastructure**"
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    progress_embed.add_field(
        name="🖥️ OS",
        value="**Ubuntu 22.04**" if os_type == "ubuntu" else "**Debian 12**",
        inline=True
    )

    progress_embed.add_field(
        name="🧠 RAM",
        value=f"**{ram.upper()}**",
        inline=True
    )

    progress_embed.add_field(
        name="⚡ CPU",
        value=f"**{cpu} vCPU**",
        inline=True
    )

    progress_embed.add_field(
        name="💾 Disk",
        value=f"**{disk}**",
        inline=True
    )

    progress_embed.add_field(
        name="⏳ Expiry",
        value="**Permanent**",
        inline=True
    )

    progress_embed.set_footer(
        text="☁️ KingCloud • Free VPS Manager"
    )

    await interaction.followup.send(
        embed=progress_embed,
        ephemeral=True
    )

    # ─────────────────────────────────────────────
    # CREATE DOCKER VPS
    # ─────────────────────────────────────────────

    hostname = f"{VPS_HOSTNAME}-{user_id}"

    suffix = random.randint(1000, 9999)

    container_name = f"{os_type}-vps-{user_id}-{suffix}"

    image = (
        "ubuntu:22.04"
        if os_type == "ubuntu"
        else "debian:bookworm"
    )

    logger.info(
        f"Deploying VPS for {user_id}: "
        f"{container_name} / {image}"
    )

    container_id = await async_docker_run(
        image,
        hostname,
        ram,
        cpu,
        disk,
        container_name
    )

    if not container_id:
        embed = discord.Embed(
            title="❌ Deployment Failed",
            description=(
                "KingCloud could not create your VPS container.\n\n"
                "Please try again later."
            ),
            color=discord.Color.red()
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True
        )
        return

    # ─────────────────────────────────────────────
    # INSTALL SSH
    # ─────────────────────────────────────────────

    await asyncio.sleep(5)

    await async_install_tmate(
        container_id,
        os_type
    )

    await asyncio.sleep(10)

    exec_process = await docker_exec_tmate(
        container_id
    )

    ssh_line = None

    if exec_process:
        ssh_line = await capture_ssh_session_line(
            exec_process
        )

    # ─────────────────────────────────────────────
    # SSH FAILURE CLEANUP
    # ─────────────────────────────────────────────

    if not ssh_line:
        logger.error(
            f"SSH generation failed for {container_id}"
        )

        await async_docker_stop(container_id)

        await asyncio.sleep(2)

        await async_docker_rm(container_id)

        embed = discord.Embed(
            title="❌ Deployment Failed",
            description=(
                "Your VPS container was created, "
                "but secure SSH access could not be prepared.\n\n"
                "The failed container has been cleaned up."
            ),
            color=discord.Color.red()
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True
        )

        return

    # ─────────────────────────────────────────────
    # SAVE VPS
    # ─────────────────────────────────────────────

    add_vps(
        user_id,
        container_id,
        container_name,
        os_type,
        hostname,
        ssh_line,
        ram,
        cpu,
        disk
    )

    # Cooldown starts only after successful VPS creation


    update_vps_status(
        container_id,
        "running"
    )

    os_name = (
        "Ubuntu 22.04"
        if os_type == "ubuntu"
        else "Debian 12"
    )

    # ─────────────────────────────────────────────
    # PREMIUM DM EMBED
    # ─────────────────────────────────────────────

    dm_embed = discord.Embed(
        title="☁️ VPS Created Successfully!",
        description=(
            "🎉 Your **KingCloud Free VPS** is ready!\n\n"
            "Your VPS has been successfully deployed "
            "and is currently running.\n\n"
            "🔐 **Your SSH access is private. "
            "Do not share it publicly.**"
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )

    dm_embed.add_field(
        name="🖥️ Operating System",
        value=f"**{os_name}**",
        inline=True
    )

    dm_embed.add_field(
        name="📡 Status",
        value="🟢 **Running**",
        inline=True
    )

    dm_embed.add_field(
        name="🧠 RAM",
        value=f"**{ram.upper()}**",
        inline=True
    )

    dm_embed.add_field(
        name="⚡ CPU",
        value=f"**{cpu} vCPU**",
        inline=True
    )

    dm_embed.add_field(
        name="💾 Disk",
        value=f"**{disk}**",
        inline=True
    )

    dm_embed.add_field(
        name="⏳ Expiry",
        value="**Permanent**",
        inline=True
    )

    dm_embed.add_field(
        name="🌐 Hostname",
        value=f"`{hostname}`",
        inline=False
    )

    dm_embed.add_field(
        name="🔐 SSH Access",
        value=f"```{ssh_line}```",
        inline=False
    )

    dm_embed.add_field(
        name="🎛️ Manage Your VPS",
        value=(
            "Use **`/manage`** to control your VPS.\n\n"
            "Available controls:\n"
            "▶️ Start  •  ⏹️ Stop  •  🔄 Restart\n"
            "♻️ Reinstall  •  🔐 Generate SSH"
        ),
        inline=False
    )

    dm_embed.set_footer(
        text="☁️ KingCloud • Free VPS Manager"
    )

    # ─────────────────────────────────────────────
    # SEND DM
    # ─────────────────────────────────────────────

    try:
        await target_user.send(
            embed=dm_embed
        )
        dm_sent = True

    except discord.Forbidden:
        dm_sent = False
        logger.warning(
            f"Cannot DM user {target_user.id}"
        )

    except Exception as e:
        dm_sent = False
        logger.error(
            f"DM failed for {target_user.id}: {e}"
        )

    # ─────────────────────────────────────────────
    # CHANNEL SUCCESS UI
    # ─────────────────────────────────────────────

    success_embed = discord.Embed(
        title="✅ Free VPS Deployed Successfully!",
        description=(
            "🎉 Your **KingCloud Free VPS** is ready!\n\n"
            "📩 **Check your DMs** for your VPS details "
            "and secure SSH access.\n\n"
            "🎛️ Use **`/manage`** anytime to control your VPS."
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )

    success_embed.add_field(
        name="📡 Status",
        value="🟢 **Running**",
        inline=True
    )

    success_embed.add_field(
        name="⏳ Expiry",
        value="**Permanent**",
        inline=True
    )

    if not dm_sent:
        success_embed.add_field(
            name="⚠️ DM Notice",
            value=(
                "I couldn't send the VPS details to your DMs.\n"
                "Please enable DMs from server members."
            ),
            inline=False
        )

    success_embed.set_footer(
        text="☁️ KingCloud • Free VPS Manager"
    )

    await interaction.followup.send(
        embed=success_embed,
        ephemeral=True
    )

    logger.info(
        f"VPS deployed successfully: "
        f"user={user_id}, container={container_id}"
    )

# Admin helpers
class AdminVPSManagerView(discord.ui.View):
    def __init__(self, admin_id, target_user, vps_list):
        super().__init__(timeout=300)
        self.admin_id = admin_id
        self.target_user = target_user
        self.vps_list = vps_list
        self.selected_vps = None

        options = []
        for vps in vps_list[:25]:
            status = str(vps["status"]).lower()
            emoji = "🟢" if status == "running" else "🔴"
            name = str(vps["container_name"])[:80]
            options.append(
                discord.SelectOption(
                    label=name,
                    value=str(vps["container_id"]),
                    description=f"{status.title()} • {vps['os_type']}"[:100],
                    emoji=emoji
                )
            )

        self.vps_select = discord.ui.Select(
            placeholder="🖥️ Select a VPS to manage...",
            options=options,
            custom_id="admin_vps_select"
        )
        self.vps_select.callback = self.select_vps
        self.add_item(self.vps_select)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message(
                "⛔ This admin panel belongs to another administrator.",
                ephemeral=True
            )
            return False
        return True

    async def select_vps(self, interaction):
        self.selected_vps = self.vps_select.values[0]

        vps = next(
            (v for v in self.vps_list if str(v["container_id"]) == self.selected_vps),
            None
        )

        if not vps:
            await interaction.response.send_message(
                "❌ VPS not found.",
                ephemeral=True
            )
            return

        status = str(vps["status"]).lower()
        suspended = bool(vps["suspended"])

        embed = discord.Embed(
            title="🛠️ Admin VPS Manager",
            description=(
                f"**Target:** {self.target_user.mention}\n\n"
                f"🖥️ **VPS:** `{vps['container_name']}`\n"
                f"💻 **OS:** `{vps['os_type']}`\n"
                f"📊 **Status:** {'🟢 Running' if status == 'running' else '🔴 Stopped'}\n"
                f"🔐 **Suspended:** {'Yes' if suspended else 'No'}\n\n"
                "**Select an action below:**"
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(
            text=f"{WATERMARK} • Admin Control Panel",
            icon_url=bot.user.avatar.url if bot.user.avatar else None
        )

        await interaction.response.edit_message(embed=embed, view=self)

    async def run_action(self, interaction, action):
        if not self.selected_vps:
            await interaction.response.send_message(
                "⚠️ Please select a VPS first.",
                ephemeral=True
            )
            return

        vps = next(
            (v for v in self.vps_list if str(v["container_id"]) == self.selected_vps),
            None
        )

        if not vps:
            await interaction.response.send_message(
                "❌ VPS not found.",
                ephemeral=True
            )
            return

        container_id = vps["container_id"]
        success = False

        await interaction.response.defer()

        if action == "start":
            success = await async_docker_start(container_id)
            if success:
                update_vps_status(container_id, "running")

        elif action == "stop":
            success = await async_docker_stop(container_id)
            if success:
                update_vps_status(container_id, "stopped")

        elif action == "restart":
            success = await async_docker_restart(container_id)
            if success:
                update_vps_status(container_id, "running")

        elif action == "suspend":
            success = await async_docker_stop(container_id)
            if success:
                update_vps_status(container_id, "stopped")
                update_vps_suspended(container_id, 1)

        elif action == "unsuspend":
            update_vps_suspended(container_id, 0)
            success = True

        elif action == "delete":
            await async_docker_stop(container_id)
            await asyncio.sleep(2)
            success = await async_docker_rm(container_id)
            if success:
                delete_vps(container_id)

        if success:
            names = {
                "start": "Started",
                "stop": "Stopped",
                "restart": "Restarted",
                "suspend": "Suspended",
                "unsuspend": "Unsuspended",
                "delete": "Deleted"
            }

            embed = discord.Embed(
                title="✅ Admin Action Completed",
                description=(
                    f"**Action:** `{names.get(action, action.title())}`\n"
                    f"**VPS:** `{vps['container_name']}`\n"
                    f"**User:** {self.target_user.mention}\n\n"
                    "The VPS management panel remains available below."
                ),
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc)
            )
        else:
            embed = discord.Embed(
                title="❌ Action Failed",
                description=(
                    f"Could not perform `{action}` on "
                    f"`{vps['container_name']}`."
                ),
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )

        embed.set_footer(
            text=f"{WATERMARK} • Admin Control Panel",
            icon_url=bot.user.avatar.url if bot.user.avatar else None
        )

        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="Start", emoji="🟢", style=discord.ButtonStyle.success, row=1)
    async def start_btn(self, interaction, button):
        await self.run_action(interaction, "start")

    @discord.ui.button(label="Stop", emoji="🔴", style=discord.ButtonStyle.danger, row=1)
    async def stop_btn(self, interaction, button):
        await self.run_action(interaction, "stop")

    @discord.ui.button(label="Restart", emoji="🔄", style=discord.ButtonStyle.primary, row=1)
    async def restart_btn(self, interaction, button):
        await self.run_action(interaction, "restart")

    @discord.ui.button(label="Suspend", emoji="🔒", style=discord.ButtonStyle.secondary, row=2)
    async def suspend_btn(self, interaction, button):
        await self.run_action(interaction, "suspend")

    @discord.ui.button(label="Unsuspend", emoji="🔓", style=discord.ButtonStyle.success, row=2)
    async def unsuspend_btn(self, interaction, button):
        await self.run_action(interaction, "unsuspend")

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def delete_btn(self, interaction, button):
        await self.run_action(interaction, "delete")


@bot.tree.command(name="admin-manage", description="Admin: Open VPS management panel for a user")
@app_commands.describe(target_user="The user whose VPS you want to manage")
@app_commands.guild_only()
async def admin_manage(interaction: discord.Interaction, target_user: discord.User):
    if not is_admin(interaction.user):
        embed = discord.Embed(
            title="⛔ Access Denied",
            description="This command is restricted to administrators.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM vps WHERE user_id = ? ORDER BY created_at DESC",
        (target_user.id,)
    )
    vps_list = cursor.fetchall()
    conn.close()

    if not vps_list:
        embed = discord.Embed(
            title="📭 No VPS Found",
            description=f"{target_user.mention} does not have any VPS instances.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.set_footer(text=WATERMARK)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title="🛠️ Admin VPS Manager",
        description=(
            f"**Target User:** {target_user.mention}\n"
            f"**VPS Count:** `{len(vps_list)}`\n\n"
            "Select a VPS from the menu below to manage it.\n\n"
            "🔐 **Admin-only control panel**"
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_thumbnail(
        url=target_user.display_avatar.url
    )
    embed.set_footer(
        text=f"{WATERMARK} • Admin Control Panel",
        icon_url=bot.user.avatar.url if bot.user.avatar else None
    )

    view = AdminVPSManagerView(
        interaction.user.id,
        target_user,
        vps_list
    )

    await interaction.response.send_message(
        embed=embed,
        view=view,
        ephemeral=True
    )

async def admin_kill_all(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.defer()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT container_id FROM vps WHERE status = "running"')
    running = cursor.fetchall()
    conn.close()
    stopped = 0
    for row in running:
        cid = row['container_id']
        if await async_docker_stop(cid):
            update_vps_status(cid, "stopped")
            stopped += 1
            logger.info(f"Stopped {cid}")
    embed = discord.Embed(title="Admin: Kill All Running VPS", description=f"Successfully stopped {stopped} running VPS instances.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.followup.send(embed=embed)


class AdminListView(discord.ui.View):
    def __init__(self, interaction_user_id, all_vps):
        super().__init__(timeout=180)
        self.user_id = interaction_user_id
        self.all_vps = all_vps
        self.category = "all"
        self.page = 0
        self.per_page = 8

        self.update_components()

    def filtered_vps(self):
        if self.category == "running":
            return [
                v for v in self.all_vps
                if v["status"] == "running" and not v["suspended"]
            ]

        if self.category == "stopped":
            return [
                v for v in self.all_vps
                if v["status"] == "stopped" and not v["suspended"]
            ]

        if self.category == "suspended":
            return [
                v for v in self.all_vps
                if v["suspended"]
            ]

        return self.all_vps

    def total_pages(self):
        total = len(self.filtered_vps())
        return max(1, (total + self.per_page - 1) // self.per_page)

    def build_embed(self):
        vps = self.filtered_vps()
        pages = self.total_pages()

        if self.page >= pages:
            self.page = pages - 1

        start = self.page * self.per_page
        current = vps[start:start + self.per_page]

        running = sum(
            1 for v in self.all_vps
            if v["status"] == "running" and not v["suspended"]
        )
        suspended = sum(1 for v in self.all_vps if v["suspended"])
        stopped = sum(
            1 for v in self.all_vps
            if v["status"] == "stopped" and not v["suspended"]
        )

        category_names = {
            "all": "📋 All VPS Instances",
            "running": "🟢 Running VPS",
            "stopped": "🔴 Stopped VPS",
            "suspended": "🟡 Suspended VPS"
        }

        embed = discord.Embed(
            title=f"🛡️ {category_names[self.category]}",
            description=(
                f"**KingCloud Free VPS Manager — Admin Panel**\n\n"
                f"📦 **Total:** `{len(self.all_vps)}`  "
                f"🟢 **Running:** `{running}`  "
                f"🔴 **Stopped:** `{stopped}`  "
                f"🟡 **Suspended:** `{suspended}`\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📄 **Page {self.page + 1} / {pages}**"
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )

        if bot.user:
            embed.set_author(
                name=bot.user.name,
                icon_url=bot.user.display_avatar.url
            )

        if not current:
            embed.add_field(
                name="No VPS Found",
                value="There are no VPS instances in this category.",
                inline=False
            )

        for v in current:
            status = v["status"]
            suspended_flag = v["suspended"]

            if suspended_flag:
                icon = "🟡"
                status_text = "Suspended"
            elif status == "running":
                icon = "🟢"
                status_text = "Running"
            else:
                icon = "🔴"
                status_text = "Stopped"

            username = v["username"] or "Unknown User"
            container_name = v["container_name"] or "Unknown"
            hostname = v["hostname"] or "N/A"
            os_type = v["os_type"] or "Unknown"

            embed.add_field(
                name=f"{icon} {username}",
                value=(
                    f"**VPS:** `{container_name}`\n"
                    f"**Status:** `{status_text}`\n"
                    f"**OS:** `{os_type.capitalize()}`\n"
                    f"**Hostname:** `{hostname}`\n"
                    f"**Resources:** `{v['ram']} RAM` • `{v['cpu']} CPU` • `{v['disk']} Disk`"
                ),
                inline=False
            )

        embed.set_footer(
            text=f"{WATERMARK} • Admin VPS List • Page {self.page + 1}/{pages}",
            icon_url=bot.user.display_avatar.url if bot.user else None
        )

        return embed

    def update_components(self):
        self.clear_items()

        options = [
            discord.SelectOption(
                label="All VPS",
                value="all",
                emoji="📋",
                default=self.category == "all"
            ),
            discord.SelectOption(
                label="Running",
                value="running",
                emoji="🟢",
                default=self.category == "running"
            ),
            discord.SelectOption(
                label="Stopped",
                value="stopped",
                emoji="🔴",
                default=self.category == "stopped"
            ),
            discord.SelectOption(
                label="Suspended",
                value="suspended",
                emoji="🟡",
                default=self.category == "suspended"
            )
        ]

        select = discord.ui.Select(
            placeholder="📂 Select VPS Category",
            options=options,
            row=0
        )
        select.callback = self.category_callback
        self.add_item(select)

        pages = self.total_pages()

        previous = discord.ui.Button(
            label="Previous",
            emoji="◀️",
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0,
            row=1
        )
        previous.callback = self.previous_callback

        page_button = discord.ui.Button(
            label=f"Page {self.page + 1}/{pages}",
            emoji="📄",
            style=discord.ButtonStyle.primary,
            disabled=True,
            row=1
        )

        next_button = discord.ui.Button(
            label="Next",
            emoji="▶️",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= pages - 1,
            row=1
        )
        next_button.callback = self.next_callback

        self.add_item(previous)
        self.add_item(page_button)
        self.add_item(next_button)

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⛔ This admin panel belongs to another user.",
                ephemeral=True
            )
            return False
        return True

    async def category_callback(self, interaction: discord.Interaction):
        self.category = interaction.data["values"][0]
        self.page = 0
        self.update_components()

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self
        )

    async def previous_callback(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1

        self.update_components()

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self
        )

    async def next_callback(self, interaction: discord.Interaction):
        if self.page < self.total_pages() - 1:
            self.page += 1

        self.update_components()

        await interaction.response.edit_message(
            embed=self.build_embed(),
            view=self
        )


@bot.tree.command(
    name="admin-list",
    description="Admin: Browse all VPS instances"
)
@app_commands.guild_only()
async def admin_list(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        embed = discord.Embed(
            description="⛔ **This command is restricted to administrators only.**",
            color=discord.Color.red()
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT
            v.*,
            COALESCE(u.username, 'Unknown User') AS username
        FROM vps v
        LEFT JOIN users u ON u.user_id = v.user_id
        ORDER BY
            CASE WHEN v.status = 'running' THEN 0 ELSE 1 END,
            v.created_at DESC
    """)

    rows = cursor.fetchall()
    conn.close()

    all_vps = [dict(row) for row in rows]

    view = AdminListView(
        interaction.user.id,
        all_vps
    )

    await interaction.response.send_message(
        embed=view.build_embed(),
        view=view
    )

@bot.tree.command(name="admin-stats", description="Admin: View bot statistics")
@app_commands.guild_only()
async def admin_stats(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        embed = discord.Embed(
            title="⛔  ACCESS DENIED",
            description="Only server administrators can use this command.",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM users")
    num_users = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM vps")
    num_vps = cursor.fetchone()[0]

    cursor.execute('SELECT COUNT(*) FROM vps WHERE status="running"')
    num_running = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM bans")
    num_banned = cursor.fetchone()[0]

    cursor.execute('SELECT ram, cpu, disk FROM vps WHERE status="running"')
    rows = cursor.fetchall()

    total_cpu = sum(float(row["cpu"]) for row in rows)
    total_ram = sum(parse_gb(row["ram"]) for row in rows)
    total_disk = sum(parse_gb(row["disk"]) for row in rows)

    conn.close()

    offline = max(0, num_vps - num_running)
    capacity = TOTAL_SERVER_LIMIT
    available = max(0, capacity - num_running)

    embed = discord.Embed(
        title="☁️  KINGCLOUD ADMIN DASHBOARD",
        description=(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 **Live infrastructure overview**\n"
            "Monitor users, VPS capacity and allocated resources.\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    if bot.user and bot.user.avatar:
        embed.set_author(
            name="KingCloud Free VPS Manager",
            icon_url=bot.user.avatar.url
        )
        embed.set_thumbnail(url=bot.user.avatar.url)

    embed.add_field(
        name="👥 USERS",
        value=(
            f"**Total**  `{num_users}`\n"
            f"**Banned** `{num_banned}`"
        ),
        inline=True
    )

    embed.add_field(
        name="🖥️ VPS",
        value=(
            f"**Total**   `{num_vps}`\n"
            f"**Running** `{num_running}`\n"
            f"**Offline** `{offline}`"
        ),
        inline=True
    )

    embed.add_field(
        name="📦 CAPACITY",
        value=(
            f"**Limit** `{capacity}` VPS\n"
            f"**Used**  `{num_running}/{capacity}`\n"
            f"**Free**  `{available}`"
        ),
        inline=True
    )

    embed.add_field(
        name="⚡ CPU ALLOCATED",
        value=f"**{total_cpu:.1f} cores**",
        inline=True
    )

    embed.add_field(
        name="🧠 RAM ALLOCATED",
        value=f"**{total_ram:.1f} GB**",
        inline=True
    )

    embed.add_field(
        name="💾 DISK ALLOCATED",
        value=f"**{total_disk:.1f} GB**",
        inline=True
    )

    usage = (num_running / capacity * 100) if capacity else 0

    embed.add_field(
        name="📈 SERVER UTILIZATION",
        value=f"**{usage:.1f}%**  •  `{num_running}/{capacity}` VPS",
        inline=False
    )

    embed.add_field(
        name="🟢 STATUS",
        value="**ONLINE** • All systems operational",
        inline=False
    )

    embed.set_footer(
        text=f"{WATERMARK} • Live Admin Dashboard",
        icon_url=bot.user.avatar.url if bot.user and bot.user.avatar else None
    )

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-delete-user", description="Admin: Delete all VPS for a user")
@app_commands.describe(target_user="The target user")
@app_commands.guild_only()
async def admin_delete_user(interaction: discord.Interaction, target_user: discord.User):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await interaction.response.defer()
    user_id = target_user.id
    vps_list = get_user_vps(user_id)
    deleted = 0
    for vps in vps_list:
        container_id = vps['container_id']
        await async_docker_stop(container_id)
        await asyncio.sleep(2)
        await async_docker_rm(container_id)
        delete_vps(container_id)
        deleted += 1
        logger.info(f"Deleted VPS {container_id} for user {user_id}")
    embed = discord.Embed(description=f"Deleted {deleted} VPS instances for {target_user}.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="admin-ban", description="Admin: Ban a user from creating VPS")
@app_commands.describe(target_user="The target user")
@app_commands.guild_only()
async def admin_ban(interaction: discord.Interaction, target_user: discord.User):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    add_ban(target_user.id)
    embed = discord.Embed(
        title="🔨  VPS ACCESS BANNED",
        description=(
            f"**KingCloud • Administrator Console**\n\n"
            f"VPS creation access has been **blocked** for {target_user.mention}."
        ),
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(
        name="👤 User",
        value=f"{target_user.mention}\n`{target_user.id}`",
        inline=False
    )
    embed.add_field(
        name="🔒 Access",
        value="🚫 **VPS Deployment Blocked**",
        inline=True
    )
    embed.add_field(
        name="📌 Status",
        value="**Banned**",
        inline=True
    )
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(
        text="KingCloud Free VPS Manager • Administrator Console",
        icon_url=bot.user.display_avatar.url if bot.user else None
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-unban", description="Admin: Unban a user")
@app_commands.describe(target_user="The target user")
@app_commands.guild_only()
async def admin_unban(interaction: discord.Interaction, target_user: discord.User):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    remove_ban(target_user.id)
    embed = discord.Embed(
        title="🔓  VPS ACCESS RESTORED",
        description=(
            f"**KingCloud • Administrator Console**\n\n"
            f"VPS creation access has been **restored** for {target_user.mention}."
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(
        name="👤 User",
        value=f"{target_user.mention}\n`{target_user.id}`",
        inline=False
    )
    embed.add_field(
        name="🔓 Access",
        value="✅ **VPS Deployment Allowed**",
        inline=True
    )
    embed.add_field(
        name="📌 Status",
        value="**Unbanned**",
        inline=True
    )
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(
        text="KingCloud Free VPS Manager • Administrator Console",
        icon_url=bot.user.display_avatar.url if bot.user else None
    )
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-vps-info", description="Admin: View full VPS details for a user")
@app_commands.describe(target_user="The target user", vps_identifier="VPS ID or Name")
@app_commands.guild_only()
async def admin_vps_info(interaction: discord.Interaction, target_user: discord.User, vps_identifier: str):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="VPS not found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    container_id = vps['container_id']
    uptime = get_uptime(container_id)
    stats = get_stats(container_id)
    os_name = "Ubuntu 22.04" if vps['os_type'] == "ubuntu" else "Debian 12"
    embed = discord.Embed(title=f"{target_user.name} - VPS Details: {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="OS", value=os_name, inline=True)
    embed.add_field(name="Hostname", value=vps['hostname'], inline=True)
    embed.add_field(name="Status", value=vps['status'], inline=True)
    embed.add_field(name="Suspended", value="Yes" if vps['suspended'] else "No", inline=True)
    embed.add_field(name="Container ID", value=f"```{container_id}```", inline=False)
    embed.add_field(name="Allocated Resources", value=f"{vps['ram']} RAM | {vps['cpu']} CPU | {vps['disk']} Disk", inline=False)
    embed.add_field(name="Current Usage", value=f"CPU: {stats['cpu']} | Mem: {stats['mem']}", inline=False)
    embed.add_field(name="Uptime", value=uptime, inline=True)
    embed.add_field(name="Network I/O", value=stats['net'], inline=False)
    embed.add_field(name="Created At", value=vps['created_at'], inline=True)
    if vps['ssh_command']:
        ssh_trunc = vps['ssh_command'][:100] + "..." if len(vps['ssh_command']) > 100 else vps['ssh_command']
        embed.add_field(name="SSH Command", value=f"```{ssh_trunc}```", inline=False)
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="admin-logs", description="Admin: View logs for a user's VPS")
@app_commands.describe(target_user="The target user", vps_identifier="VPS ID or Name", lines="Number of lines (default 50)")
@app_commands.guild_only()
async def admin_logs(interaction: discord.Interaction, target_user: discord.User, vps_identifier: str, lines: int = 50):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    vps = get_vps_by_identifier(target_user.id, vps_identifier)
    if not vps:
        embed = discord.Embed(description="VPS not found.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
        return
    container_id = vps['container_id']
    logs = get_logs(container_id, lines)
    embed = discord.Embed(title=f"Logs for {target_user.name}'s {vps['container_name']}", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.set_author(name=bot.user.name, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    embed.add_field(name="Recent Logs", value=f"```{logs}```", inline=False)
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed)

# Show bot & developer information
@bot.tree.command(name="about", description="Show bot and developer information")
async def about(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 VPS Manager Bot • About",
        description=(
            "**KingCloud Free VPS Manager** is a powerful and user-friendly "
            "Discord bot designed for VPS deployment, management, automation, "
            "and Docker-based infrastructure.\n\n"
            "Built with **speed, stability, security, and simplicity** in mind."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(
        name="📌 Bot Information",
        value=(
            "➜ **Name:** KingCloud Free VPS Manager\n"
            "➜ **Version:** V1.0\n"
            "➜ **Framework:** Python • discord.py\n"
            "➜ **Uptime:** 🟢 Online & Stable\n"
            "➜ **Purpose:** VPS Deployment & Management\n"
            "➜ **Infrastructure:** Docker Containers"
        ),
        inline=False
    )

    embed.add_field(
        name="⚡ Features",
        value=(
            "• 🚀 Automated VPS Deployment\n"
            "• 🎛️ Complete VPS Management\n"
            "• 🐳 Docker Container Management\n"
            "• 🔐 SSH Session Generation\n"
            "• ♻️ VPS Reinstallation\n"
            "• 📊 VPS Status & Usage Information\n"
            "• 📝 VPS Logs & Monitoring\n"
            "• 🛡️ Admin Management Tools"
        ),
        inline=False
    )

    embed.add_field(
        name="👨‍💻 Meet the Developer • MrPain / Pain09X",
        value=(
            "**MrPain / Pain09X** is the developer and maintainer behind "
            "**KingCloud Free VPS Manager**.\n\n"
            "Focused on building reliable, fast, and user-friendly "
            "infrastructure systems with an emphasis on **performance, "
            "automation, security, and clean UI/UX**.\n\n"
            "🔹 **Development Focus**\n"
            "• VPS & Server Management\n"
            "• Docker & Containerization\n"
            "• Discord Bot Development\n"
            "• VPS Automation & Provisioning\n"
            "• Advanced Management Panels\n"
            "• QEMU Virtual Machines\n"
            "• Server Monitoring\n"
            "• Minecraft Hosting & Optimization\n\n"
            "🚀 **What I Build**\n"
            "Infrastructure tools and automation systems that make "
            "server management easier, faster, and more accessible.\n\n"
            "🛡️ **Development Principles**\n"
            "• Clean & maintainable code\n"
            "• Reliable automation\n"
            "• Performance optimization\n"
            "• Secure infrastructure\n"
            "• Simple & modern user experience"
        ),
        inline=False
    )

    embed.add_field(
        name="💡 About KingCloud",
        value=(
            "KingCloud is continuously improved with new features, "
            "performance optimizations, security improvements, and better "
            "VPS management tools.\n\n"
            "Thank you for using **KingCloud Free VPS Manager!** ☁️"
        ),
        inline=False
    )

    embed.set_footer(
        text="Built with ❤️ by MrPain / Pain09X • KingCloud Free VPS Manager",
        icon_url=bot.user.avatar.url if bot.user and bot.user.avatar else None
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

@bot.tree.command(name="logs", description="View recent activity logs for your VPS")
@app_commands.describe(lines="Number of log entries (default 25)")
async def user_logs(interaction: discord.Interaction, lines: int = 25):
    lines = max(1, min(int(lines), 50))

    vps = get_vps_by_identifier(interaction.user.id, None)

    if not vps:
        await interaction.response.send_message(
            embed=kc_embed(
                "📋  VPS LOGS",
                "❌ You don't have a VPS.",
                discord.Color.red()
            ),
            ephemeral=True
        )
        return

    try:
        rows = get_vps_audit_logs(
            interaction.user.id,
            vps["container_id"],
            lines
        )

        embed = discord.Embed(
            title="📋  VPS ACTIVITY",
            description=f"Recent activity for **{vps['container_name']}**",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )

        if not rows:
            embed.add_field(
                name="📝 Activity",
                value="No VPS activity has been recorded yet.",
                inline=False
            )
        else:
            entries = []

            for row in rows:
                action = row["action"]
                details = row["details"] or ""
                created = row["created_at"]

                text = f"**{action}**"
                if details:
                    text += f" — {details}"

                text += f"\n`{created}`"
                entries.append(text)

            # Discord embed field limit safety
            chunks = []
            current = ""

            for entry in entries:
                if len(current) + len(entry) + 2 > 3900:
                    chunks.append(current)
                    current = entry
                else:
                    current = entry if not current else current + "\n\n" + entry

            if current:
                chunks.append(current)

            for i, chunk in enumerate(chunks[:3]):
                embed.add_field(
                    name="📝 Activity" if i == 0 else "📝 Activity • Continued",
                    value=chunk,
                    inline=False
                )

        embed.set_footer(
            text=WATERMARK,
            icon_url=bot.user.avatar.url if bot.user and bot.user.avatar else None
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

    except Exception:
        logger.exception("Failed to load VPS activity logs")
        await interaction.response.send_message(
            embed=kc_embed(
                "❌  LOG ERROR",
                "Could not load VPS activity logs.",
                discord.Color.red()
            ),
            ephemeral=True
        )

@bot.tree.command(
    name="add-deploy-cooldown",
    description="Add a 30-minute Free VPS deployment cooldown"
)
@app_commands.describe(user="User whose deployment cooldown you want to add")
@app_commands.guild_only()
async def add_deploy_cooldown(interaction: discord.Interaction, user: discord.User):

    if not is_admin(interaction.user):
        await interaction.response.send_message(
            embed=kc_embed(
                "⛔  ACCESS DENIED",
                "Only **KingCloud administrators** can use this command.",
                discord.Color.red()
            ),
            ephemeral=True
        )
        return

    try:
        import sqlite3
        import time

        db_path = DATABASE_FILE
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS deploy_cooldowns (
                user_id INTEGER PRIMARY KEY,
                last_deploy REAL NOT NULL
            )
        """)

        cur.execute("""
            INSERT INTO deploy_cooldowns (user_id, last_deploy)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET last_deploy = excluded.last_deploy
        """, (int(user.id), time.time()))

        conn.commit()
        conn.close()

        # Keep in-memory cooldown state consistent if used elsewhere.
        free_vps_cooldowns[user.id] = time.time()

        embed = discord.Embed(
            title="⏳ DEPLOYMENT COOLDOWN ADDED",
            description=(
                f"A **30-minute deployment cooldown** has been added "
                f"for {user.mention}.\n\n"
                "They cannot use `/deploy` until the cooldown expires."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )

        embed.add_field(
            name="👤 User",
            value=f"{user.mention}\n`{user.id}`",
            inline=False
        )

        embed.add_field(
            name="⏱️ Cooldown",
            value="**30 Minutes**",
            inline=True
        )

        embed.add_field(
            name="📋 Status",
            value="**Active**",
            inline=True
        )

        if bot.user:
            embed.set_thumbnail(url=bot.user.display_avatar.url)
            embed.set_footer(
                text="KingCloud Free VPS Manager",
                icon_url=bot.user.display_avatar.url
            )
        else:
            embed.set_footer(text="KingCloud Free VPS Manager")

        await interaction.response.send_message(
            embed=embed,
            ephemeral=False
        )

    except Exception as e:
        logger.exception("add-deploy-cooldown failed")
        await interaction.response.send_message(
            embed=kc_embed(
                "❌  COOLDOWN ERROR",
                f"Failed to add deployment cooldown.\n`{e}`",
                discord.Color.red()
            ),
            ephemeral=True
        )

@bot.tree.command(
    name="remove-deploy-cooldown",
    description="Remove a user's Free VPS deployment cooldown"
)
@app_commands.describe(user="User whose cooldown you want to remove")
async def remove_deploy_cooldown(interaction: discord.Interaction, user: discord.User):

    # Admin only
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            embed=kc_embed(
                "⛔ ACCESS DENIED",
                "Only server administrators can use this command.",
                discord.Color.red()
            ),
            ephemeral=True
        )
        return

    try:
        import sqlite3

        db_path = DATABASE_FILE
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        cur.execute(
            "DELETE FROM deploy_cooldowns WHERE user_id = ?",
            (user.id,)
        )

        conn.commit()
        removed = cur.rowcount
        conn.close()

        # Also clear current in-memory cooldown
        free_vps_cooldowns.pop(user.id, None)

        if removed:
            title = "✅ COOLDOWN REMOVED"
            description = (
                f"Deployment cooldown removed for {user.mention}.\n\n"
                "They can now use `/deploy` again."
            )
            color = discord.Color.green()
        else:
            title = "ℹ️ NO COOLDOWN"
            description = (
                f"{user.mention} does not currently have a saved cooldown."
            )
            color = discord.Color.blurple()

        await interaction.response.send_message(
            embed=kc_embed(title, description, color),
            ephemeral=True
        )

    except Exception as e:
        logger.exception("Remove deploy cooldown failed")
        await interaction.response.send_message(
            embed=kc_embed(
                "❌ ERROR",
                f"Could not remove the cooldown.\n`{e}`",
                discord.Color.red()
            ),
            ephemeral=True
        )

@bot.tree.command(
    name="deploy",
    description="Deploy a permanent KingCloud Free VPS"
)
async def deploy(interaction: discord.Interaction):

    # ── 30 MINUTE COOLDOWN ──────────────────────
    import time
    user_id = interaction.user.id
    now = time.time()
    last_deploy = get_deploy_cooldown(user_id)
    remaining = DEPLOY_COOLDOWN_SECONDS - (now - last_deploy)

    if remaining > 0:
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)

        embed = kc_embed(
            "⏳  DEPLOYMENT COOLDOWN",
            (
                "You recently created a Free VPS.\n\n"
                f"Please wait **{minutes}m {seconds}s** before deploying again."
            ),
            discord.Color.orange()
        )
        embed.add_field(
            name="🕒 Cooldown",
            value="**30 Minutes**",
            inline=True
        )
        embed.add_field(
            name="⏱️ Available In",
            value=f"**{minutes}m {seconds}s**",
            inline=True
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )
        return

    # 30-minute cooldown — checked BEFORE OS selection
    import time
    user_id = interaction.user.id
    last_deploy = get_deploy_cooldown(user_id)
    remaining = DEPLOY_COOLDOWN_SECONDS - (time.time() - last_deploy)

    if last_deploy > 0 and remaining > 0:
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)

        embed = kc_embed(
            "⏳  DEPLOYMENT COOLDOWN",
            (
                "You recently created a Free VPS.\n\n"
                f"Please wait **{minutes}m {seconds}s** before deploying another VPS."
            ),
            discord.Color.orange()
        )
        embed.add_field(
            name="🕒 Cooldown",
            value="**30 Minutes**",
            inline=True
        )
        embed.add_field(
            name="⏱️ Available In",
            value=f"**{minutes}m {seconds}s**",
            inline=True
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )
        return

    if get_vps_by_identifier(interaction.user.id, None):
        await interaction.response.send_message(
            embed=kc_embed(
                "⚠️  VPS ALREADY EXISTS",
                "You already have a VPS.\nUse `/manage` to control it.",
                discord.Color.orange()
            ),
            ephemeral=True
        )
        return

    if is_banned(interaction.user.id):
        await interaction.response.send_message(
            embed=kc_embed(
                "⛔  ACCESS DENIED",
                "You are not allowed to deploy a VPS.",
                discord.Color.red()
            ),
            ephemeral=True
        )
        return

    if get_total_instances() >= TOTAL_SERVER_LIMIT:
        await interaction.response.send_message(
            embed=kc_embed(
                "⚠️  NO FREE SLOTS",
                f"All **{TOTAL_SERVER_LIMIT}** VPS slots are currently allocated.",
                discord.Color.orange()
            ),
            ephemeral=True
        )
        return

    e = kc_embed(
        "☁️  DEPLOY FREE VPS",
        "Choose an operating system to continue.",
        discord.Color.blurple()
    )
    e.add_field(
        name="📦 AVAILABLE OS",
        value="🐧 **Ubuntu 22.04**\n🌀 **Debian 12**",
        inline=False
    )
    e.add_field(
        name="📊 INCLUDED RESOURCES",
        value=(
            f"🧠 RAM: **{DEFAULT_RAM.upper()}**\n"
            f"⚡ CPU: **{DEFAULT_CPU} vCPU**\n"
            f"💾 Disk: **{DEFAULT_DISK}**\n"
            "⏳ Expiry: **Permanent**"
        ),
        inline=False
    )

    await interaction.response.send_message(
        embed=e,
        view=KCDeployOS(interaction.user.id)
    )





class KCDeployOS(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=180)
        self.user_id = user_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⛔ This panel belongs to another user.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Ubuntu 22.04", emoji="🐧", style=discord.ButtonStyle.primary)
    async def ubuntu(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_vps(interaction, "ubuntu")

    @discord.ui.button(label="Debian 12", emoji="🌀", style=discord.ButtonStyle.secondary)
    async def debian(self, interaction: discord.Interaction, button: discord.ui.Button):
        await create_vps(interaction, "debian")

async def kc_sshx(interaction, container_id):
    """Generate an SSHX web terminal for the VPS."""
    await interaction.response.defer(ephemeral=True)

    try:
        container = client.containers.get(str(container_id))

        install_cmd = (
            "command -v curl >/dev/null 2>&1 || "
            "(apt-get update -qq && apt-get install -y -qq curl); "
            "export PATH=/root/.local/bin:/usr/local/bin:$PATH; "
            "command -v sshx >/dev/null 2>&1 || "
            "(curl -sSf https://sshx.io/get | sh); "
            "export PATH=/root/.local/bin:/usr/local/bin:$PATH"
        )

        result = await asyncio.to_thread(
            container.exec_run,
            ["bash", "-lc", install_cmd],
            demux=False
        )

        if result.exit_code != 0:
            raise RuntimeError(
                "Failed to install SSHX."
            )

        start_cmd = (
            "export PATH=/root/.local/bin:/usr/local/bin:$PATH; "
            "pkill -x sshx 2>/dev/null || true; "
            "rm -f /tmp/kingcloud-sshx.log; "
            "nohup sshx >/tmp/kingcloud-sshx.log 2>&1 "
            "</dev/null &"
        )

        await asyncio.to_thread(
            container.exec_run,
            ["bash", "-lc", start_cmd],
            demux=False
        )

        url = None

        for _ in range(15):
            await asyncio.sleep(1)

            result = await asyncio.to_thread(
                container.exec_run,
                [
                    "bash",
                    "-lc",
                    "grep -oE 'https://sshx\\.io/s/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+' "
                    "/tmp/kingcloud-sshx.log 2>/dev/null | tail -1"
                ],
                demux=False
            )

            output = (
                result.output.decode(errors="ignore")
                if result.output else ""
            ).strip()

            if output:
                url = output.splitlines()[-1].strip()
                break

        if not url:
            raise RuntimeError(
                "SSHX started, but no session URL was returned."
            )

        await interaction.followup.send(
            embed=kc_embed(
                "🌐 SSHX SESSION",
                f"Your SSHX terminal is ready:\n\n"
                f"🔗 {url}\n\n"
                "Open the link to access your VPS terminal.",
                discord.Color.green()
            ),
            ephemeral=True
        )

    except Exception as e:
        logger.exception(
            f"SSHX generation failed for {container_id}: {e}"
        )

        await interaction.followup.send(
            embed=kc_embed(
                "❌ SSHX FAILED",
                f"Failed to generate SSHX session.\n\n`{e}`",
                discord.Color.red()
            ),
            ephemeral=True
        )

class KCManageView(discord.ui.View):
    def __init__(self, user_id, container_id):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.container_id = container_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⛔ This VPS control panel belongs to another user.",
                ephemeral=True
            )
            return False
        return True

    async def refresh_panel(self, interaction):
        vps = get_vps_by_identifier(self.user_id, self.container_id)

        if not vps:
            embed = kc_embed(
                "❌ VPS NOT FOUND",
                "This VPS no longer exists.",
                discord.Color.red()
            )
            await interaction.edit_original_response(embed=embed, view=None)
            return

        os_name = kc_os_name(vps["os_type"])
        status = kc_status(vps)

        embed = kc_embed(
            "☁️ KINGCLOUD CONTROL PANEL",
            f"Manage **{vps['container_name']}** using the controls below.",
            discord.Color.green()
            if str(vps["status"]).lower() == "running"
            else discord.Color.dark_grey()
        )

        embed.add_field(name="📡 STATUS", value=f"**{status}**", inline=False)
        embed.add_field(name="🖥️ OS", value=f"`{os_name}`", inline=True)
        embed.add_field(name="🧠 RAM", value=f"`{vps['ram']}`", inline=True)
        embed.add_field(name="⚡ CPU", value=f"`{vps['cpu']} vCPU`", inline=True)
        embed.add_field(name="💾 DISK", value=f"`{vps['disk']}`", inline=True)
        embed.add_field(
            name="🆔 VPS ID",
            value=f"`{str(vps['container_id'])[:12]}`",
            inline=True
        )
        embed.add_field(name="⏳ EXPIRY", value="**Permanent**", inline=True)

        embed.set_footer(
            text=WATERMARK,
            icon_url=bot.user.avatar.url if bot.user.avatar else None
        )

        await interaction.edit_original_response(
            embed=embed,
            view=self
        )

    async def action(self, interaction, action):
        if action == "sshx":
            await kc_sshx(
                interaction,
                self.container_id
            )
            return
        if action == "ssh":
            await regen_ssh_command(
                interaction,
                self.container_id,
                target_user=interaction.user
            )
            return

        if action == "delete":
            await interaction.response.defer(ephemeral=True)

            vps = get_vps_by_identifier(self.user_id, self.container_id)
            if not vps:
                await interaction.followup.send(
                    embed=kc_embed(
                        "❌ VPS NOT FOUND",
                        "This VPS no longer exists.",
                        discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            await async_docker_stop(self.container_id)
            await asyncio.sleep(2)
            success = await async_docker_rm(self.container_id)

            if success:
                log_vps_event(
                    interaction.user.id,
                    self.container_id,
                    "VPS DELETE",
                    f"VPS: {vps['container_name']}"
                )
                delete_vps(self.container_id)

                await interaction.followup.send(
                    embed=kc_embed(
                        "✅ VPS REMOVED",
                        "Your VPS has been removed successfully.",
                        discord.Color.green()
                    ),
                    ephemeral=True
                )
            else:
                await interaction.followup.send(
                    embed=kc_embed(
                        "❌ DELETE FAILED",
                        "Failed to remove the VPS.",
                        discord.Color.red()
                    ),
                    ephemeral=True
                )
            return

        if action == "reinstall":
            vps = get_vps_by_identifier(self.user_id, self.container_id)
            if not vps:
                await interaction.response.send_message(
                    embed=kc_embed(
                        "❌ VPS NOT FOUND",
                        "This VPS no longer exists.",
                        discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            await reinstall_vps(
                interaction,
                self.container_id,
                vps["os_type"],
                target_user=interaction.user
            )
            return

        await manage_vps(
            interaction,
            self.container_id,
            action,
            target_user=interaction.user
        )

    @discord.ui.button(
        label="Start",
        emoji="🟢",
        style=discord.ButtonStyle.success,
        row=0
    )
    async def start_btn(self, interaction, button):
        await self.action(interaction, "start")

    @discord.ui.button(
        label="Stop",
        emoji="🔴",
        style=discord.ButtonStyle.danger,
        row=0
    )
    async def stop_btn(self, interaction, button):
        await self.action(interaction, "stop")

    @discord.ui.button(
        label="Restart",
        emoji="🔄",
        style=discord.ButtonStyle.primary,
        row=0
    )
    async def restart_btn(self, interaction, button):
        await self.action(interaction, "restart")

    @discord.ui.button(
        label="Tmate",
        emoji="🔐",
        style=discord.ButtonStyle.secondary,
        row=1
    )
    async def ssh_btn(self, interaction, button):
        await self.action(interaction, "ssh")

    @discord.ui.button(
        label="SSHX",
        emoji="🌐",
        style=discord.ButtonStyle.success,
        row=1
    )
    async def sshx_btn(self, interaction, button):
        await self.action(interaction, "sshx")

    @discord.ui.button(
        label="Reinstall",
        emoji="♻️",
        style=discord.ButtonStyle.secondary,
        row=1
    )
    async def reinstall_btn(self, interaction, button):
        await self.action(interaction, "reinstall")

    @discord.ui.button(
        label="Delete",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
        row=1
    )
    async def delete_btn(self, interaction, button):
        await self.action(interaction, "delete")

@bot.tree.command(
    name="manage",
    description="Open your KingCloud VPS control panel"
)
@app_commands.describe(vps_identifier="VPS ID or name (optional)")
async def manage(interaction: discord.Interaction, vps_identifier: str = None):
    vps = get_vps_by_identifier(
        interaction.user.id,
        vps_identifier
    )

    if not vps:
        await interaction.response.send_message(
            embed=kc_embed(
                "❌  VPS NOT FOUND",
                "You don't have a VPS.\nUse `/deploy` to create one.",
                discord.Color.red()
            ),
            ephemeral=True
        )
        return

    os_name = kc_os_name(vps["os_type"])

    e = kc_embed(
        "☁️  KINGCLOUD CONTROL PANEL",
        f"Manage **{vps['container_name']}** using the controls below.",
        discord.Color.green()
        if vps["status"] == "running"
        else discord.Color.dark_grey()
    )

    e.add_field(
        name="📡 STATUS",
        value=f"**{kc_status(vps)}**",
        inline=False
    )
    e.add_field(name="🖥️ OS", value=f"`{os_name}`", inline=True)
    e.add_field(name="🧠 RAM", value=f"`{vps['ram']}`", inline=True)
    e.add_field(name="⚡ CPU", value=f"`{vps['cpu']} vCPU`", inline=True)
    e.add_field(name="💾 DISK", value=f"`{vps['disk']}`", inline=True)
    e.add_field(name="🆔 VPS ID", value=f"`{vps['container_id'][:12]}`", inline=True)
    e.add_field(name="⏳ EXPIRY", value="**Permanent**", inline=True)

    await interaction.response.send_message(
        embed=e,
        view=KCManageView(
            interaction.user.id,
            vps["container_id"]
        )
    )

@bot.tree.command(name="admin-create", description="Admin: Create a VPS for a user with optional custom resources")
@app_commands.describe(target_user="The target user", os_type="OS type", ram="RAM e.g. 2g (optional)", cpu="CPU cores (optional)", disk="Disk e.g. 20G (optional)")
@app_commands.choices(os_type=[
    app_commands.Choice(name="Ubuntu", value="ubuntu"),
    app_commands.Choice(name="Debian", value="debian")
])
async def admin_create(interaction: discord.Interaction, target_user: discord.User, os_type: str, ram: str = None, cpu: str = None, disk: str = None):
    if not is_admin(interaction.user):
        embed = discord.Embed(description="This command is restricted to admins only.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    ram = ram or DEFAULT_RAM
    cpu = cpu or DEFAULT_CPU
    disk = disk or DEFAULT_DISK
    if get_total_instances() >= TOTAL_SERVER_LIMIT:
        embed = discord.Embed(description=f"Global server limit reached: {TOTAL_SERVER_LIMIT} total running instances.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    await create_vps(interaction, os_type, ram, cpu, disk, target_user=target_user)

@bot.tree.command(name="vps-info", description="View full details of your VPS")
@app_commands.describe(vps_identifier="VPS ID or Name (defaults to first)")
async def vps_info(interaction: discord.Interaction, vps_identifier: str = None):
    await interaction.response.defer(ephemeral=True)

    try:
        vps = get_vps_by_identifier(interaction.user.id, vps_identifier)

        if not vps:
            embed = discord.Embed(
                title="❌ VPS NOT FOUND",
                description="You don't have a VPS.\nUse `/deploy` to create one.",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        status = vps["status"]
        running = status == "running"

        embed = discord.Embed(
            title="☁️  KINGCLOUD VPS",
            description=(
                f"**{vps['container_name']}**\n"
                f"{'🟢' if running else '🔴'} **{status.upper()}**"
            ),
            color=discord.Color.green() if running else discord.Color.red()
        )

        os_name = (
            "Ubuntu 22.04"
            if vps["os_type"] == "ubuntu"
            else "Debian 12"
        )

        embed.add_field(
            name="🖥️ SYSTEM",
            value=(
                f"**OS**  `{os_name}`\n"
                f"**Host**  `{vps['hostname']}`"
            ),
            inline=True
        )

        embed.add_field(
            name="⚡ RESOURCES",
            value=(
                f"🧠 `{vps['ram']}` RAM\n"
                f"⚡ `{vps['cpu']} CPU`\n"
                f"💾 `{vps['disk']}` Disk"
            ),
            inline=True
        )

        embed.add_field(
            name="🆔 VPS ID",
            value=f"`{vps['container_id'][:12]}`",
            inline=False
        )

        ssh_command = vps["ssh_command"]
        if ssh_command:
            embed.add_field(
                name="🔐 SSH ACCESS",
                value=f"```{ssh_command}```",
                inline=False
            )

        embed.set_footer(
            text="KingCloud • Free VPS Manager",
            icon_url=bot.user.display_avatar.url if bot.user else None
        )

        await interaction.followup.send(
            embed=embed,
            ephemeral=True
        )

    except Exception as e:
        embed = discord.Embed(
            title="❌ VPS INFO ERROR",
            description=f"Could not load VPS information.\n`{str(e)[:500]}`",
            color=discord.Color.red()
        )
        await interaction.followup.send(
            embed=embed,
            ephemeral=True
        )

@bot.tree.command(name="list", description="List all your VPS instances")
async def list_vps(interaction: discord.Interaction):
    vps_list = get_user_vps(interaction.user.id)
    if not vps_list:
        embed = discord.Embed(description="You have no VPS instances.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    embed = discord.Embed(
        title="☁️ Your VPS Instances",
        description=(
            f"Manage your **KingCloud Free VPS** instances from here.\n"
            f"📦 **Total VPS:** `{len(vps_list)}`"
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    if bot.user.avatar:
        embed.set_author(
            name="KingCloud Free VPS Manager",
            icon_url=bot.user.avatar.url
        )

    for vps in vps_list[:25]:
        status = str(vps['status']).lower()
        status_emoji = "🟢" if status == "running" else "🔴"
        status_text = "Running" if status == "running" else status.title()

        uptime = get_uptime(vps['container_id'])
        suspended_text = " • ⚠️ Suspended" if vps['suspended'] else ""

        embed.add_field(
            name=f"{status_emoji} {vps['container_name']}",
            value=(
                f"**OS:** `{vps['os_type']}`{suspended_text}\n"
                f"**Status:** {status_emoji} `{status_text}`\n"
                f"**Uptime:** `{uptime}`\n"
                f"**Resources:** `{vps['ram']} RAM` • `{vps['cpu']} CPU` • `{vps['disk']} Disk`\n"
                f"**Hostname:** `{vps['hostname']}`\n"
                f"**VPS ID:** `{vps['container_id']}`"
            ),
            inline=False
        )

    embed.set_footer(
        text=f"{WATERMARK} • Select a VPS to manage it",
        icon_url=bot.user.avatar.url if bot.user.avatar else None
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )

@bot.tree.command(name="remove", description="Remove your VPS instance")
@app_commands.describe(vps_identifier="VPS ID or Name")
async def remove_vps(interaction: discord.Interaction, vps_identifier: str):
    await interaction.response.defer(ephemeral=True)
    vps = get_vps_by_identifier(interaction.user.id, vps_identifier)

    log_vps_event(
        interaction.user.id,
        vps["container_id"],
        "VPS DELETE",
        f"VPS: {vps['container_name']}"
    )

    if not vps:
        embed = discord.Embed(description="VPS not found.", color=discord.Color.red())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    log_vps_event(
        interaction.user.id,
        vps["container_id"],
        "VPS DELETE",
        f"VPS: {vps['container_name']}"
    )

    container_id = vps['container_id']
    await async_docker_stop(container_id)
    await asyncio.sleep(2)
    await async_docker_rm(container_id)
    delete_vps(container_id)
    embed = discord.Embed(title="VPS Removed Successfully", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.followup.send(embed=embed, ephemeral=True)

# Admin commands
@bot.tree.command(name="admin-kill-all", description="Admin: Stop all running VPS instances")
@app_commands.guild_only()
async def admin_kill_all_cmd(interaction: discord.Interaction):
    await admin_kill_all(interaction)

@bot.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(title="🏓 Pong!", description=f"Latency: {latency}ms", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=WATERMARK, icon_url=bot.user.avatar.url if bot.user.avatar else None)
    await interaction.response.send_message(embed=embed, ephemeral=True)


class HelpCategoryView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.category = "home"
        self.build()

    def build(self):
        self.clear_items()

        if self.category == "home":
            options=[
                discord.SelectOption(
                    label="User Commands",
                    description="Manage and control your VPS",
                    emoji="📚",
                    value="user"
                ),
                discord.SelectOption(
                    label="Admin Commands",
                    description="Advanced VPS administration",
                    emoji="📚",
                    value="admin"
                )
            ]
        elif self.category == "user":
            options=[
                discord.SelectOption(
                    label="User Commands",
                    description="Currently viewing user commands",
                    emoji="📚",
                    value="user",
                    default=True
                ),
                discord.SelectOption(
                    label="Admin Commands",
                    description="View administrator commands",
                    emoji="📚",
                    value="admin"
                )
            ]
        else:
            options=[
                discord.SelectOption(
                    label="User Commands",
                    description="View VPS user commands",
                    emoji="📚",
                    value="user"
                ),
                discord.SelectOption(
                    label="Admin Commands",
                    description="Currently viewing admin commands",
                    emoji="📚",
                    value="admin",
                    default=True
                )
            ]

        select=discord.ui.Select(
            placeholder="Choose a command category...",
            options=options,
            row=0
        )
        select.callback=self.category_callback
        self.add_item(select)

        if self.category != "home":
            home=discord.ui.Button(
                label="Overview",
                emoji="📚",
                style=discord.ButtonStyle.secondary,
                row=1
            )
            home.callback=self.home_callback
            self.add_item(home)

        close=discord.ui.Button(
            label="Close",
            emoji="📚",
            style=discord.ButtonStyle.danger,
            row=1
        )
        close.callback=self.close_callback
        self.add_item(close)

    def embed(self):
        if self.category=="home":
            embed=discord.Embed(
                title="☁️  KingCloud",
                description=(
                    "**Free VPS Manager**\n"
                    "A modern Discord interface for managing your VPS infrastructure.\n\n"
                    "━━━━━━━━━━━━━━━━━━━━\n\n"
                    "👤  **User Panel**\n"
                    "Manage, monitor and control your own VPS.\n\n"
                    "🛡️  **Admin Panel**\n"
                    "Powerful tools for VPS administration.\n\n"
                    "Select a category below to get started."
                ),
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc)
            )
        elif self.category=="user":
            embed=discord.Embed(
                title="👤  User Commands",
                description=(
                    "**KingCloud • VPS Control**\n"
                    "Everything you need to manage your VPS.\n\n"
                    "━━━━━━━━━━━━━━━━━━━━"
                ),
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(
                name="🚀 VPS Management",
                value=(
                    "`/deploy` — Deploy a new KingCloud Free VPS\n"
                    "`/list` — List all your VPS instances\n"
                    "`/manage` — Open your VPS management panel\n"
                    "`/remove` — Remove your VPS instance\n"
                    "`/vps-info` — View full details of your VPS"
                ),
                inline=False
            )
            embed.add_field(
                name="📊 Monitoring",
                value=(
                    "`/logs` — View recent logs for your VPS\n"
                    "`/ping` — Check the bot's latency"
                ),
                inline=False
            )
            embed.add_field(
                name="ℹ️ Information",
                value=(
                    "`/about` — Show bot and developer information\n"
                    "`/help` — Open this help center"
                ),
                inline=False
            )
        else:
            if int(self.user_id) != 1353572110592643076:
                return discord.Embed(
                    title="🛡️ Admin Access",
                    description="⛔  **Administrator permissions required.**",
                    color=discord.Color.red()
                )

            embed=discord.Embed(
                title="🛡️  Admin Commands",
                description=(
                    "**KingCloud • Administrator Console**\n"
                    "Advanced tools for managing the VPS platform.\n\n"
                    "━━━━━━━━━━━━━━━━━━━━"
                ),
                color=discord.Color.dark_gold(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(
                name="⚙️ VPS Administration",
                value=(
                    "`/admin-create` — Create a VPS for a user\n"
                    "`/admin-manage` — Manage a user's VPS\n"
                    "`/admin-list` — Browse all VPS instances\n"
                    "`/admin-vps-info` — View full VPS details\n"
                    "`/admin-logs` — View logs for a user's VPS\n"
                    "`/admin-delete-user` — Delete all VPS for a user\n"
                    "`/admin-add <user>` — Grant Admin + User command access\n"
                    "`/admin-remove <user>` — Remove Admin access from a user"
                ),
                inline=False
            )
            embed.add_field(
                name="📈 Platform",
                value=(
                    "`/admin-list-users` — View users & VPS counts\n"
                    "`/admin-stats` — View bot/platform statistics\n"
                    "`/admin-kill-all` — Stop all running VPS instances"
                ),
                inline=False
            )
            embed.add_field(
                name="🔐 Access Control",
                value=(
                    "`/admin-ban` — Ban a user from creating VPS\n"
                    "`/admin-unban` — Unban a user"
                ),
                inline=False
            )
            embed.add_field(
                name="⏱️ Deployment Control",
                value=(
                    "`/add-deploy-cooldown <user>` — Add deployment cooldown\n"
                    "`/remove-deploy-cooldown <user>` — Remove a user's Free VPS deployment cooldown"
                ),
                inline=False
            )
        if bot.user:
            embed.set_thumbnail(url=bot.user.display_avatar.url)

        embed.set_footer(
            text="KingCloud Free VPS Manager • Secure • Fast • Reliable",
            icon_url=bot.user.display_avatar.url if bot.user else None
        )

        return embed

    async def category_callback(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⛔ This help panel belongs to another user.",
                ephemeral=True
            )
            return

        self.category=interaction.data["values"][0]

        if self.category=="admin" and not is_admin(interaction.user):
            await interaction.response.send_message(
                "⛔ **Admin access required.**",
                ephemeral=True
            )
            return

        self.build()
        await interaction.response.edit_message(
            embed=self.embed(),
            view=self
        )

    async def home_callback(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⛔ This help panel belongs to another user.",
                ephemeral=True
            )
            return

        self.category="home"
        self.build()

        await interaction.response.edit_message(
            embed=self.embed(),
            view=self
        )

    async def close_callback(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "⛔ This help panel belongs to another user.",
                ephemeral=True
            )
            return

        await interaction.message.delete()



def safe_help_embed(embed):
    """Keep Help embeds within Discord API limits."""
    try:
        # Discord limits
        if embed.description and len(embed.description) > 4096:
            embed.description = embed.description[:4093] + "..."

        # Limit fields
        while len(embed.fields) > 25:
            embed.remove_field(len(embed.fields) - 1)

        # Limit individual fields
        for i, field in enumerate(list(embed.fields)):
            name = str(field.name)[:256]
            value = str(field.value)[:1021] + "..." if len(str(field.value)) > 1024 else str(field.value)
            embed.set_field_at(
                i,
                name=name or "Information",
                value=value or "No information available.",
                inline=field.inline
            )

        # Limit footer
        if embed.footer and embed.footer.text and len(embed.footer.text) > 2048:
            embed.set_footer(text=embed.footer.text[:2045] + "...")

        # Keep total embed payload comfortably below Discord's 6000-char limit.
        total = 0
        if embed.title:
            total += len(embed.title)
        if embed.description:
            total += len(embed.description)
        if embed.footer and embed.footer.text:
            total += len(embed.footer.text)

        for i, field in enumerate(list(embed.fields)):
            total += len(str(field.name)) + len(str(field.value))
            if total > 5800:
                remaining = max(50, 5800 - (total - len(str(field.value))))
                value = str(field.value)[:remaining - 3] + "..."
                embed.set_field_at(
                    i,
                    name=str(field.name)[:256],
                    value=value,
                    inline=field.inline
                )
                break

    except Exception:
        pass

    return embed

@bot.tree.command(name="help", description="Open the KingCloud help center")
async def help_cmd(interaction: discord.Interaction):
    view=HelpCategoryView(interaction.user.id)

    await interaction.response.send_message(
        embed=safe_help_embed(view.embed()),
        view=view,
        ephemeral=True
    )


# Events
@bot.event
async def on_ready():
    logger.info(f"Bot ready: {bot.user}")

    try:
        await change_status()
        if not change_status.is_running():
            change_status.start()
        logger.info("Discord presence/status started")
    except Exception as e:
        logger.error(f"Discord presence startup failed: {e}")

    try:
        for guild in bot.guilds:
            try:
                bot.tree.clear_commands(guild=guild)
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                logger.info(
                    f"Guild synced {len(synced)} commands to "
                    f"{guild.name} ({guild.id})"
                )
            except Exception as guild_error:
                logger.error(
                    f"Guild sync failed for {guild.id}: {guild_error}"
                )
    except Exception as e:
        logger.error(f"Command sync failed: {e}")


@tasks.loop(seconds=1)
async def change_status():
    try:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM vps WHERE suspended = 0 AND status = 'running'")
        running = cursor.fetchone()[0] or 0
        conn.close()

        total_slots = int(os.getenv("TOTAL_SERVER_LIMIT", TOTAL_SERVER_LIMIT))
        activity = f"🟢 {running} Running | 📦 {total_slots} Total Slots"
        logger.info(f"DISCORD PRESENCE: {activity}")

        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=activity
            )
        )
    except Exception as e:
        logger.error(f"Status update failed: {e}")

@bot.tree.command(name="admin-add", description="Grant Admin + User command access")
@app_commands.describe(user="User to grant admin access")
@app_commands.guild_only()
async def admin_add(interaction: discord.Interaction, user: discord.Member):
    if int(interaction.user.id) != ADMIN_ID:
        await interaction.response.send_message(
            "⛔  **Administrator permissions required.**",
            ephemeral=True
        )
        return

    init_admin_users()
    conn = get_db_connection()

    existing = conn.execute(
        "SELECT 1 FROM admin_users WHERE user_id = ?",
        (int(user.id),)
    ).fetchone()

    if existing:
        conn.close()
        await interaction.response.send_message(
            f"ℹ️  {user.mention} is already an administrator.",
            ephemeral=True
        )
        return

    conn.execute(
        "INSERT INTO admin_users (user_id) VALUES (?)",
        (int(user.id),)
    )
    conn.commit()
    conn.close()

    embed = discord.Embed(
        title="🛡️  ADMIN ACCESS GRANTED",
        description=(
            f"Administrator access has been granted to {user.mention}.\n\n"
            "**KingCloud • Administrator Console**"
        ),
        color=discord.Color.green(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(
        name="👤 New Administrator",
        value=f"{user.mention}\n`{user.id}`",
        inline=False
    )

    embed.add_field(
        name="🔓 Access",
        value="All **User + Admin** commands",
        inline=True
    )

    embed.add_field(
        name="💾 Status",
        value="**Persistent**",
        inline=True
    )

    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
        embed.set_footer(
            text="KingCloud Free VPS Manager",
            icon_url=bot.user.display_avatar.url
        )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=False
    )


@bot.tree.command(name="admin-remove", description="Remove Admin access from a user")
@app_commands.describe(user="User to remove from administrators")
@app_commands.guild_only()
async def admin_remove(interaction: discord.Interaction, user: discord.Member):
    if int(interaction.user.id) != ADMIN_ID:
        await interaction.response.send_message(
            "⛔  **Main administrator permissions required.**",
            ephemeral=True
        )
        return

    if int(user.id) == ADMIN_ID:
        await interaction.response.send_message(
            "⛔  **You cannot remove the main administrator.**",
            ephemeral=True
        )
        return

    init_admin_users()
    conn = get_db_connection()

    existing = conn.execute(
        "SELECT 1 FROM admin_users WHERE user_id = ?",
        (int(user.id),)
    ).fetchone()

    if not existing:
        conn.close()
        await interaction.response.send_message(
            f"ℹ️  {user.mention} is **not an administrator**.",
            ephemeral=True
        )
        return

    conn.execute(
        "DELETE FROM admin_users WHERE user_id = ?",
        (int(user.id),)
    )
    conn.commit()
    conn.close()

    embed = discord.Embed(
        title="🔒 ADMIN ACCESS REMOVED",
        description=(
            f"Administrator access has been removed from {user.mention}.\n\n"
            "**KingCloud • Administrator Console**"
        ),
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(
        name="👤 User",
        value=f"{user.mention}\n`{user.id}`",
        inline=False
    )

    embed.add_field(
        name="🔓 Access",
        value="Admin commands **removed**",
        inline=True
    )

    embed.add_field(
        name="📋 Status",
        value="**Removed**",
        inline=True
    )

    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
        embed.set_footer(
            text="KingCloud Free VPS Manager",
            icon_url=bot.user.display_avatar.url
        )
    else:
        embed.set_footer(text="KingCloud Free VPS Manager")

    await interaction.response.send_message(
        embed=embed,
        ephemeral=False
    )

init_admin_users()

if __name__ == "__main__":
    if not TOKEN:
        logger.error("TOKEN not set in .env")
        sys.exit(1)

    bot.run(TOKEN)
