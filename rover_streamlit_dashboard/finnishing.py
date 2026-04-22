"""
rover_combined.py
-----------------
Combines three components into one running system:

  1. LiDAR scanning (PyRPlidar, runs in a background thread)
  2. MAVSDK telemetry check (GPS, battery, flight mode, armed state)
  3. MAVSDK mission execution with real-time obstacle avoidance

How it works:
  - The LiDAR thread continuously scans and writes the latest
    obstacle decision (FORWARD / TURN_LEFT / TURN_RIGHT / STOP)
    into a shared thread-safe variable.
  - The MAVSDK async loop connects to the Pixhawk, prints telemetry,
    uploads the mission, and then starts it.
  - A separate async monitoring task reads the LiDAR decision every
    0.3 seconds. If an obstacle is detected, it pauses the mission.
    When the path clears, it resumes the mission automatically.

LiDAR decision logic:
  - If front is clear                    -> FORWARD
  - If front is blocked                  -> compare left vs right,
                                            go toward the bigger open area
  - If all three are below FRONT_STOP_CM -> STOP

Obstacle response summary:
  - FORWARD             -> mission continues normally
  - TURN_LEFT           -> pause mission, offboard left yaw, resume scan
  - TURN_RIGHT          -> pause mission, offboard right yaw, resume scan
  - STOP (front only)   -> pause mission, count checks, reverse (manual)
                           once FRONT_BLOCK_LIMIT is hit
  - STOP (all blocked)  -> pause mission, offboard reverse immediately
"""

import asyncio
import logging
import threading
import time

from pyrplidar import PyRPlidar
from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan
from mavsdk.offboard import VelocityBodyYawspeed
from db_writer import insert_telemetry, insert_log

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LiDAR settings
# ---------------------------------------------------------------------------

LIDAR_PORT = "/dev/ttyUSB0"   # Change to your actual port on Raspberry Pi
LIDAR_BAUD = 115200
LIDAR_PWM  = 660

# Distance thresholds in centimetres — tune these before each run
FRONT_STOP_CM  = 200   # If nearest front obstacle is closer than this -> stop
LEFT_STOP_CM   = 140    # If left obstacle is closer than this -> do not turn left
LEFT_CLEAR_CM  = 140   # Left must have at least this much space to turn there
RIGHT_STOP_CM  = 140    # If right obstacle is closer than this -> do not turn right
RIGHT_CLEAR_CM = 140   # Right must have at least this much space to turn there

# Minimum points per zone before making a decision (lower = faster response)
MIN_POINTS_PER_ZONE = 3

# Give up collecting and decide with whatever we have after this many total points
MAX_SCAN_POINTS = 200

# ---------------------------------------------------------------------------
# MAVSDK settings
# ---------------------------------------------------------------------------

SERIAL_ADDRESS = "serial:///dev/ttyAMA0:57600"

# ---------------------------------------------------------------------------
# Mission waypoints  <-- CHANGE THESE before every run
# Format: (latitude, longitude)
# ---------------------------------------------------------------------------

WAYPOINTS = [
(66.480577300, 25.721500200),
(66.480397600, 25.721434800),

]

# Speed in m/s applied to every waypoint (keep low until tuning is done)
ROVER_SPEED = 0.5

# How long the rover waits at each waypoint before moving to the next one (seconds)
WAYPOINT_HOLD_SECS = 4.0

# How long the rover reverses when blocked (seconds)
REVERSE_SECS = 10.0

# How long the rover turns left or right during offboard avoidance (seconds)
TURN_SECS = 3.0

# Yaw rate used during offboard turns (degrees per second)
TURN_YAW_RATE = 45.0

# Manual control input rate — must be faster than COM_RC_LOSS_T in QGC (seconds)
MANUAL_INPUT_INTERVAL = 0.1   # 10 Hz; safe default for COM_RC_LOSS_T = 0.5s

# How many consecutive front-blocked checks before forcing a reverse
FRONT_BLOCK_LIMIT = 1

d_speed = 0.1
d_speed_r = 0.2


# ---------------------------------------------------------------------------
# Shared state between the LiDAR thread and the async mission loop
# ---------------------------------------------------------------------------

_lidar_lock  = threading.Lock()
_lidar_state = {"decision": "FORWARD", "front": 0.0, "left": 0.0, "right": 0.0}


def set_lidar_state(decision, front, left, right):
    """Thread-safe write called by the LiDAR thread."""
    with _lidar_lock:
        _lidar_state["decision"] = decision
        _lidar_state["front"]    = front
        _lidar_state["left"]     = left
        _lidar_state["right"]    = right


def get_lidar_state():
    """Thread-safe read called by the async mission monitor."""
    with _lidar_lock:
        return dict(_lidar_state)

# ===========================================================================
# DB
# ==========================================================================
def write_log(message: str):
    """Print to normal logger and also store in DB log table."""
    log.info(message)
    try:
        insert_log(message)
    except Exception as exc:
        log.error("[DB] insert_log failed: %s", exc)


async def collect_telemetry_snapshot(drone: System):
    """
    Read one quick telemetry snapshot for DB storage.
    Returns a dict with gps, battery, mode, armed.
    """
    gps_satellites = None
    battery_percent = None
    battery_voltage = None
    flight_mode = None
    armed = None

    try:
        async for gps in drone.telemetry.gps_info():
            gps_satellites = gps.num_satellites
            break
    except Exception:
        pass

    try:
        async for battery in drone.telemetry.battery():
            pct = battery.remaining_percent
            battery_percent = round(pct * 100, 1) if pct <= 1.0 else round(pct, 1)
            battery_voltage = battery.voltage_v
            break
    except Exception:
        pass

    try:
        async for mode in drone.telemetry.flight_mode():
            flight_mode = str(mode)
            break
    except Exception:
        pass

    try:
        async for is_armed in drone.telemetry.armed():
            armed = is_armed
            break
    except Exception:
        pass

    return {
        "gps_satellites": gps_satellites,
        "battery_percent": battery_percent,
        "battery_voltage": battery_voltage,
        "flight_mode": flight_mode,
        "armed": armed,
    }


async def write_telemetry_row(drone: System, decision: str, front_mm: float, left_mm: float, right_mm: float):
    """
    Store one telemetry row in public.rover_telemetry.
    Latitude/longitude/speed are left None for now unless you add position later.
    """
    try:
        snap = await collect_telemetry_snapshot(drone)

        insert_telemetry(
            lat=None,
            lon=None,
            speed=None,
            battery_percent=snap["battery_percent"],
            voltage=snap["battery_voltage"],
            front=front_mm,
            left=left_mm,
            right=right_mm,
            gps_sat=snap["gps_satellites"],
            decision=decision,
        )
    except Exception as exc:
        log.error("[DB] insert_telemetry failed: %s", exc)



# ===========================================================================
# COMPONENT 1 — LiDAR scanning (runs in its own thread)
# ===========================================================================

def in_front(angle):
    """Front zone: 330-360 and 0-30 degrees."""
    return angle >= 330 or angle <= 30

def in_left(angle):
    """Left zone: 30-90 degrees."""
    return 30 < angle <= 90

def in_right(angle):
    """Right zone: 270-330 degrees."""
    return 270 <= angle < 330

def get_min_distance(distances, default=99999):
    """Return the minimum valid (> 0) distance, or a large default if empty."""
    valid = [d for d in distances if d > 0]
    return min(valid) if valid else default

def decide_direction(front_min, left_min, right_min):
    """
    Obstacle avoidance decision logic.
    LiDAR returns mm — convert to cm here for comparison.

    If front is clear -> FORWARD.
    If front is blocked -> compare left vs right and go toward the bigger
    open area regardless of thresholds. This means:
      front=100, right=150, left=200 -> TURN_LEFT  (left is biggest)
      front=100, right=200, left=150 -> TURN_RIGHT (right is biggest)
    STOP only when all three zones are below FRONT_STOP_CM.
    """
    front_cm = front_min / 10
    left_cm  = left_min  / 10
    right_cm = right_min / 10

    if front_cm > FRONT_STOP_CM:
        return "FORWARD"

    # Front is blocked — go toward the side with more space,
    # but only if that side also clears its own stop and clear thresholds
    left_ok  = left_cm  > LEFT_STOP_CM  and left_cm  >= LEFT_CLEAR_CM
    right_ok = right_cm > RIGHT_STOP_CM and right_cm >= RIGHT_CLEAR_CM

    if left_ok and right_ok:
        # Both sides clear — pick the bigger one
        return "TURN_LEFT" if left_cm >= right_cm else "TURN_RIGHT"
    if left_ok:
        return "TURN_LEFT"
    if right_ok:
        return "TURN_RIGHT"

    # All zones are too tight
    return "STOP"


def lidar_worker(stop_event: threading.Event):
    """
    Runs in a background thread.
    Continuously reads LiDAR scan data and updates the shared state.
    Exits cleanly when stop_event is set.

    Speed optimisation:
      - Breaks out of the scan loop as soon as MIN_POINTS_PER_ZONE points
        are collected in every zone.
      - Falls back to whatever points are available after MAX_SCAN_POINTS
        total readings so a sparse zone (e.g. only 180 deg coverage) never
        causes the decision to stall for seconds.
    """
    lidar = PyRPlidar()

    try:
        log.info("[LiDAR] Connecting on %s at %d baud...", LIDAR_PORT, LIDAR_BAUD)
        lidar.connect(port=LIDAR_PORT, baudrate=LIDAR_BAUD, timeout=3)

        log.info("[LiDAR] Device info: %s", lidar.get_info())
        log.info("[LiDAR] Health: %s",      lidar.get_health())

        lidar.set_motor_pwm(LIDAR_PWM)
        log.info("[LiDAR] Motor started. Beginning scan...")

        scan_generator = lidar.force_scan()

        while not stop_event.is_set():
            front_dist, left_dist, right_dist = [], [], []
            total_points = 0

            for scan in scan_generator():
                if stop_event.is_set():
                    break

                angle    = scan.angle
                distance = scan.distance
                total_points += 1

                if distance > 0:
                    if in_front(angle):
                        front_dist.append(distance)
                    elif in_left(angle):
                        left_dist.append(distance)
                    elif in_right(angle):
                        right_dist.append(distance)

                # Break as soon as every zone has enough points
                if (len(front_dist) >= MIN_POINTS_PER_ZONE and
                    len(left_dist)  >= MIN_POINTS_PER_ZONE and
                    len(right_dist) >= MIN_POINTS_PER_ZONE):
                    break

                # Timeout fallback — decide with whatever we have
                if total_points >= MAX_SCAN_POINTS:
                    break

            front_min = get_min_distance(front_dist)
            left_min  = get_min_distance(left_dist)
            right_min = get_min_distance(right_dist)
            decision  = decide_direction(front_min, left_min, right_min)

            set_lidar_state(decision, front_min, left_min, right_min)

            # log.info(
            #     "[LiDAR] Front: %5.1f cm | Left: %5.1f cm | Right: %5.1f cm | Decision: %s",
            #     front_min / 10, left_min / 10, right_min / 10, decision
            # )
            
            msg = (
            f"[LiDAR] Front: {front_min / 10:.1f} cm | "
            f"Left: {left_min / 10:.1f} cm | "
            f"Right: {right_min / 10:.1f} cm | "
            f"Decision: {decision}"
        )
        write_log(msg)

    except Exception as exc:
        log.error("[LiDAR] Error in worker thread: %s", exc)

    finally:
        log.info("[LiDAR] Shutting down...")
        try: lidar.set_motor_pwm(0)
        except: pass
        try: lidar.stop()
        except: pass
        try: lidar.disconnect()
        except: pass
        log.info("[LiDAR] Disconnected.")


# ===========================================================================
# COMPONENT 2 — MAVSDK telemetry check
# ===========================================================================

# async def print_telemetry(drone: System):
#     """
#     Reads and prints one snapshot of GPS, battery, flight mode,
#     and armed state. Called once after connecting to the Pixhawk.
#     """
#     log.info("[Telemetry] Reading vehicle info...")

#     async for gps in drone.telemetry.gps_info():
#         log.info("[Telemetry] GPS satellites visible: %d", gps.num_satellites)
#         break

#     async for battery in drone.telemetry.battery():
#         pct = battery.remaining_percent
#         display_pct = round(pct * 100, 1) if pct <= 1.0 else round(pct, 1)
#         log.info("[Telemetry] Battery: %s %%  |  Voltage: %.2f V", display_pct, battery.voltage_v)
#         break

#     async for mode in drone.telemetry.flight_mode():
#         log.info("[Telemetry] Flight mode: %s", mode)
#         break

#     async for armed in drone.telemetry.armed():
#         log.info("[Telemetry] Armed: %s", armed)
#         break

async def print_telemetry(drone: System):
    """
    Reads and prints one snapshot of GPS, battery, flight mode,
    and armed state. Called once after connecting to the Pixhawk.
    """
    write_log("[Telemetry] Reading vehicle info...")

    async for gps in drone.telemetry.gps_info():
        write_log(f"[Telemetry] GPS satellites visible: {gps.num_satellites}")
        break

    async for battery in drone.telemetry.battery():
        pct = battery.remaining_percent
        display_pct = round(pct * 100, 1) if pct <= 1.0 else round(pct, 1)
        write_log(f"[Telemetry] Battery: {display_pct} % | Voltage: {battery.voltage_v:.2f} V")
        break

    async for mode in drone.telemetry.flight_mode():
        write_log(f"[Telemetry] Flight mode: {mode}")
        break

    async for armed in drone.telemetry.armed():
        write_log(f"[Telemetry] Armed: {armed}")
        break

# ===========================================================================
# COMPONENT 3 — MAVSDK mission execution
# ===========================================================================

def build_mission():
    """
    Builds a MissionPlan from the WAYPOINTS list defined at the top of this file.

    Key rover-specific choices:
      - is_fly_through = False  -> rover stops at each waypoint before moving on.
      - altitude = 0.0          -> rovers ignore altitude; 0 makes intent clear.
    """
    def wp(lat, lon):
        return MissionItem(
            lat, lon,
            0.0,           # altitude — ignored by PX4 rover firmware
            ROVER_SPEED,   # speed from top-level constant
            False,                              # is_fly_through = False for rover
            float("nan"), float("nan"),         # gimbal pitch / yaw (not used)
            MissionItem.CameraAction.NONE,
            WAYPOINT_HOLD_SECS, float("nan"),   # loiter time / camera photo interval
            float("nan"), float("nan"), float("nan"),
            MissionItem.VehicleAction.NONE,
        )

    items = [wp(lat, lon) for lat, lon in WAYPOINTS]
    log.info("[Mission] Built %d waypoints from WAYPOINTS constant.", len(items))
    return MissionPlan(items)


# async def print_mission_progress(drone: System):
#     """Async task: logs mission progress (current / total waypoints)."""
#     async for progress in drone.mission.mission_progress():
#         log.info(
#             "[Mission] Progress: %d / %d",
#             progress.current, progress.total
#         )


async def print_mission_progress(drone: System):
    """Async task: logs mission progress (current / total waypoints)."""
    async for progress in drone.mission.mission_progress():
        write_log(f"[Mission] Progress: {progress.current} / {progress.total}")

async def do_reverse(drone: System):
    """
    Offboard reverse — used when ALL directions are blocked.
    Sends a negative body-frame velocity for REVERSE_SECS, then stops offboard.
    """
    await drone.offboard.set_velocity_body(
        VelocityBodyYawspeed(-ROVER_SPEED, 0.0, 0.0, 0.0)
    )
    await drone.offboard.start()
    log.info("[Avoidance] Reversing (offboard) for %.1f seconds...", REVERSE_SECS)
    await asyncio.sleep(REVERSE_SECS)
    await drone.offboard.stop()
    log.info("[Avoidance] Offboard reverse done. Resuming scan...")


async def do_reverse_manual(drone: System):
    """
    Manual control reverse — used when front has been blocked FRONT_BLOCK_LIMIT
    times in a row.

    Sends x=-1.0 (full reverse) at MANUAL_INPUT_INTERVAL for REVERSE_SECS.

    ManualControl input axes:
      x: -1.0 = backwards,    +1.0 = forwards
      y: -1.0 = left,         +1.0 = right
      z:  0.0 (not used for rover)
      r: -1.0 = turn left,    +1.0 = turn right
    """
    log.info("[Avoidance] Switching to manual control for reverse...")
    await drone.manual_control.set_manual_control_input(-1.0, 0.0, 0.0, 0.0)
    await drone.manual_control.start_position_control()

    end_time = asyncio.get_event_loop().time() + REVERSE_SECS
    log.info("[Avoidance] Reversing (manual) for %.1f seconds...", REVERSE_SECS)

    while asyncio.get_event_loop().time() < end_time:
        await drone.manual_control.set_manual_control_input(-1.0, 0.0, 0.0, 0.0)
        await asyncio.sleep(MANUAL_INPUT_INTERVAL)

    await drone.manual_control.set_manual_control_input(0.0, 0.0, 0.0, 0.0)
    log.info("[Avoidance] Manual reverse done. Resuming scan...")


async def do_turn_left(drone: System):
    """
    Offboard left turn — used when LiDAR picks left as the biggest open area.
    Yaws at -TURN_YAW_RATE deg/s for TURN_SECS with zero forward velocity
    so the rover rotates in place, then stops offboard.
    """
    await drone.offboard.set_velocity_body(
        VelocityBodyYawspeed(d_speed, -d_speed_r, 0.0, -TURN_YAW_RATE)
    )
    await drone.offboard.start()
    log.info("[Avoidance] Turning left (offboard) for %.1f seconds...", TURN_SECS)
    await asyncio.sleep(TURN_SECS)
    await drone.offboard.stop()
    log.info("[Avoidance] Left turn done. Resuming scan...")


async def do_turn_right(drone: System):
    """
    Offboard right turn — used when LiDAR picks right as the biggest open area.
    Yaws at +TURN_YAW_RATE deg/s for TURN_SECS with zero forward velocity
    so the rover rotates in place, then stops offboard.
    """
    await drone.offboard.set_velocity_body(
        VelocityBodyYawspeed(d_speed, d_speed_r, 0.0, TURN_YAW_RATE)
    )
    await drone.offboard.start()
    log.info("[Avoidance] Turning right (offboard) for %.1f seconds...", TURN_SECS)
    await asyncio.sleep(TURN_SECS)
    await drone.offboard.stop()
    log.info("[Avoidance] Right turn done. Resuming scan...")


# async def monitor_obstacles(drone: System, stop_event: threading.Event):
#     """
#     Async task: checks LiDAR state every 0.3 seconds.

#     Five cases:
#       1. All directions blocked   -> pause mission, offboard reverse immediately.
#       2. Front blocked, TURN_LEFT -> pause mission, offboard left turn, resume scan.
#       3. Front blocked, TURN_RIGHT-> pause mission, offboard right turn, resume scan.
#       4. Front blocked, STOP      -> count consecutive checks. Once count hits
#                                      FRONT_BLOCK_LIMIT, manual reverse.
#       5. Front clear              -> resume mission, reset counter.
#     """
#     mission_paused    = False
#     front_block_count = 0

#     while not stop_event.is_set():
#         state     = get_lidar_state()
#         front_cm  = state["front"] / 10
#         decision  = state["decision"]
        

#         front_blocked = front_cm <= FRONT_STOP_CM
#         all_blocked   = decision == "STOP"

#         # ----------------------------------------------------------------
#         # Case 1: every direction blocked — offboard reverse immediately
#         # ----------------------------------------------------------------
#         if all_blocked and not mission_paused:
#             front_block_count = 0
#             log.warning("[Avoidance] All directions blocked. Pausing mission.")
#             try:
#                 await drone.mission.pause_mission()
#                 mission_paused = True
#                 await do_reverse(drone)
#             except Exception as exc:
#                 log.error("[Avoidance] Offboard reverse failed: %s", exc)

#         # ----------------------------------------------------------------
#         # Case 2: front blocked, left is the biggest open area
#         # ----------------------------------------------------------------
#         elif front_blocked and decision == "TURN_LEFT":
#             front_block_count = 0
#             log.warning(
#                 "[Avoidance] Front blocked at %.1f cm. Left has most space. Turning left...",
#                 front_cm
#             )
#             try:
#                 if not mission_paused:
#                     await drone.mission.pause_mission()
#                     mission_paused = True
#                 await do_turn_left(drone)
#             except Exception as exc:
#                 log.error("[Avoidance] Left turn failed: %s", exc)

#         # ----------------------------------------------------------------
#         # Case 3: front blocked, right is the biggest open area
#         # ----------------------------------------------------------------
#         elif front_blocked and decision == "TURN_RIGHT":
#             front_block_count = 0
#             log.warning(
#                 "[Avoidance] Front blocked at %.1f cm. Right has most space. Turning right...",
#                 front_cm
#             )
#             try:
#                 if not mission_paused:
#                     await drone.mission.pause_mission()
#                     mission_paused = True
#                 await do_turn_right(drone)
#             except Exception as exc:
#                 log.error("[Avoidance] Right turn failed: %s", exc)

#         # ----------------------------------------------------------------
#         # Case 4: front blocked, no clear side — count up to FRONT_BLOCK_LIMIT
#         # ----------------------------------------------------------------
#         elif front_blocked:
#             front_block_count += 1

#             if not mission_paused:
#                 log.warning(
#                     "[Avoidance] Obstacle in front at %.1f cm (limit %d cm). "
#                     "Pausing mission. Block count: %d/%d",
#                     front_cm, FRONT_STOP_CM, front_block_count, FRONT_BLOCK_LIMIT
#                 )
#                 try:
#                     await drone.mission.pause_mission()
#                     mission_paused = True
#                 except Exception as exc:
#                     log.error("[Avoidance] Could not pause mission: %s", exc)
#             else:
#                 log.warning(
#                     "[Avoidance] Still blocked at %.1f cm. Block count: %d/%d",
#                     front_cm, front_block_count, FRONT_BLOCK_LIMIT
#                 )

#             if front_block_count >= FRONT_BLOCK_LIMIT:
#                 log.warning(
#                     "[Avoidance] Front blocked %d times in a row. "
#                     "Switching to manual and reversing...",
#                     front_block_count
#                 )
#                 front_block_count = 0
#                 try:
#                     await do_reverse_manual(drone)
#                 except Exception as exc:
#                     log.error("[Avoidance] Manual reverse failed: %s", exc)

#         # ----------------------------------------------------------------
#         # Case 5: front is clear — resume mission
#         # ----------------------------------------------------------------
#         elif not front_blocked and mission_paused:
#             front_block_count = 0
#             log.info("[Avoidance] Front clear (%.1f cm). Resuming mission.", front_cm)
#             try:
#                 await drone.mission.start_mission()
#                 mission_paused = False
#             except Exception as exc:
#                 log.error("[Avoidance] Could not resume mission: %s", exc)

#         await asyncio.sleep(0.3)



async def monitor_obstacles(drone: System, stop_event: threading.Event):
    """
    Async task: checks LiDAR state every 0.3 seconds.

    Five cases:
      1. All directions blocked   -> pause mission, offboard reverse immediately.
      2. Front blocked, TURN_LEFT -> pause mission, offboard left turn, resume scan.
      3. Front blocked, TURN_RIGHT-> pause mission, offboard right turn, resume scan.
      4. Front blocked, STOP      -> count consecutive checks. Once count hits
                                     FRONT_BLOCK_LIMIT, manual reverse.
      5. Front clear              -> resume mission, reset counter.
    """
    mission_paused    = False
    front_block_count = 0

    while not stop_event.is_set():
        state     = get_lidar_state()
        front_mm  = state["front"]
        left_mm   = state["left"]
        right_mm  = state["right"]
        front_cm  = front_mm / 10
        decision  = state["decision"]

        await write_telemetry_row(
            drone=drone,
            decision=decision,
            front_mm=front_mm,
            left_mm=left_mm,
            right_mm=right_mm,
        )

        front_blocked = front_cm <= FRONT_STOP_CM
        all_blocked   = decision == "STOP"

        # ----------------------------------------------------------------
        # Case 1: every direction blocked — offboard reverse immediately
        # ----------------------------------------------------------------
        if all_blocked and not mission_paused:
            front_block_count = 0
            log.warning("[Avoidance] All directions blocked. Pausing mission.")
            try:
                await drone.mission.pause_mission()
                mission_paused = True
                await do_reverse(drone)
            except Exception as exc:
                log.error("[Avoidance] Offboard reverse failed: %s", exc)

        # ----------------------------------------------------------------
        # Case 2: front blocked, left is the biggest open area
        # ----------------------------------------------------------------
        elif front_blocked and decision == "TURN_LEFT":
            front_block_count = 0
            log.warning(
                "[Avoidance] Front blocked at %.1f cm. Left has most space. Turning left...",
                front_cm
            )
            try:
                if not mission_paused:
                    await drone.mission.pause_mission()
                    mission_paused = True
                await do_turn_left(drone)
            except Exception as exc:
                log.error("[Avoidance] Left turn failed: %s", exc)

        # ----------------------------------------------------------------
        # Case 3: front blocked, right is the biggest open area
        # ----------------------------------------------------------------
        elif front_blocked and decision == "TURN_RIGHT":
            front_block_count = 0
            log.warning(
                "[Avoidance] Front blocked at %.1f cm. Right has most space. Turning right...",
                front_cm
            )
            try:
                if not mission_paused:
                    await drone.mission.pause_mission()
                    mission_paused = True
                await do_turn_right(drone)
            except Exception as exc:
                log.error("[Avoidance] Right turn failed: %s", exc)

        # ----------------------------------------------------------------
        # Case 4: front blocked, no clear side — count up to FRONT_BLOCK_LIMIT
        # ----------------------------------------------------------------
        elif front_blocked:
            front_block_count += 1

            if not mission_paused:
                log.warning(
                    "[Avoidance] Obstacle in front at %.1f cm (limit %d cm). "
                    "Pausing mission. Block count: %d/%d",
                    front_cm, FRONT_STOP_CM, front_block_count, FRONT_BLOCK_LIMIT
                )
                try:
                    await drone.mission.pause_mission()
                    mission_paused = True
                except Exception as exc:
                    log.error("[Avoidance] Could not pause mission: %s", exc)
            else:
                log.warning(
                    "[Avoidance] Still blocked at %.1f cm. Block count: %d/%d",
                    front_cm, front_block_count, FRONT_BLOCK_LIMIT
                )

            if front_block_count >= FRONT_BLOCK_LIMIT:
                log.warning(
                    "[Avoidance] Front blocked %d times in a row. "
                    "Switching to manual and reversing...",
                    front_block_count
                )
                front_block_count = 0
                try:
                    await do_reverse_manual(drone)
                except Exception as exc:
                    log.error("[Avoidance] Manual reverse failed: %s", exc)

        # ----------------------------------------------------------------
        # Case 5: front is clear — resume mission
        # ----------------------------------------------------------------
        elif not front_blocked and mission_paused:
            front_block_count = 0
            log.info("[Avoidance] Front clear (%.1f cm). Resuming mission.", front_cm)
            try:
                await drone.mission.start_mission()
                mission_paused = False
            except Exception as exc:
                log.error("[Avoidance] Could not resume mission: %s", exc)

        await asyncio.sleep(0.3)

async def observe_mission_complete(drone: System, running_tasks: list, stop_lidar: threading.Event):
    """
    Async task: watches mission progress.

    When the rover reaches the final waypoint:
      1. Waits WAYPOINT_HOLD_SECS so the on-site hold finishes
      2. Disarms the rover (wheels stop completely)
      3. Cancels all other running async tasks
      4. Signals the LiDAR thread to exit
      5. Returns — the script closes cleanly
    """
    async for progress in drone.mission.mission_progress():
        if progress.total > 0 and progress.current == progress.total:
            log.info(
                "[Mission] Last waypoint reached (%d/%d).",
                progress.current, progress.total
            )

            log.info("[Mission] Holding at final waypoint for %.0f seconds...", WAYPOINT_HOLD_SECS)
            await asyncio.sleep(WAYPOINT_HOLD_SECS)

            log.info("[Mission] Disarming rover...")
            try:
                await drone.action.disarm()
                log.info("[Mission] Rover disarmed. Wheels stopped.")
            except Exception as exc:
                log.error("[Mission] Could not disarm: %s", exc)

            log.info("[Mission] Cancelling background tasks...")
            for task in running_tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            stop_lidar.set()
            log.info("[Mission] Mission complete. Script closing.")
            return


# ===========================================================================
# MAIN — connects everything and runs it
# ===========================================================================

async def main():
    # ----------------------------------------------------------------
    # Step 1: Start the LiDAR thread
    # ----------------------------------------------------------------
    stop_lidar   = threading.Event()
    lidar_thread = threading.Thread(
        target=lidar_worker,
        args=(stop_lidar,),
        daemon=True,
        name="LidarThread"
    )
    lidar_thread.start()
    log.info("[Main] LiDAR thread started.")
    write_log("[Main] LiDAR thread started.")

    # ----------------------------------------------------------------
    # Step 2: Connect to Pixhawk via MAVSDK
    # ----------------------------------------------------------------
    drone = System()
    log.info("[Main] Connecting to Pixhawk at %s...", SERIAL_ADDRESS)
    await drone.connect(system_address=SERIAL_ADDRESS)

    log.info("[Main] Waiting for Pixhawk connection...")
    async for state in drone.core.connection_state():
        if state.is_connected:
            log.info("[Main] Connected to Pixhawk!")
            write_log("[Main] Connected to Pixhawk!")
            break

    # ----------------------------------------------------------------
    # Step 3: Print one telemetry snapshot
    # ----------------------------------------------------------------
    await print_telemetry(drone)

    # ----------------------------------------------------------------
    # Step 4: Upload the mission
    # ----------------------------------------------------------------
    mission_plan = build_mission()
    # RTL disabled — on a rover it causes looping back to origin.
    await drone.mission.set_return_to_launch_after_mission(False)

    log.info("[Mission] Uploading mission (%d waypoints)...", len(mission_plan.mission_items))
    await drone.mission.upload_mission(mission_plan)
    log.info("[Mission] Mission uploaded.")

    # ----------------------------------------------------------------
    # Step 5: Wait for a valid global position before arming
    # ----------------------------------------------------------------
    log.info("[Mission] Waiting for global position estimate...")
    async for health in drone.telemetry.health():
        if health.is_global_position_ok and health.is_home_position_ok:
            log.info("[Mission] Global position OK.")
            break

    # ----------------------------------------------------------------
    # Step 6: Arm and start mission
    # ----------------------------------------------------------------
    log.info("[Mission] Arming...")
    write_log("[Mission] Arming...")
    await drone.action.arm()

    log.info("[Mission] Starting mission...")
    write_log("[Mission] Starting mission...")
    await drone.mission.start_mission()

    # ----------------------------------------------------------------
    # Step 7: Launch concurrent async tasks
    #   - print_mission_progress  : logs each waypoint transition
    #   - monitor_obstacles       : pauses/resumes/turns/reverses on obstacles
    #   - observe_mission_complete: disarms and exits after last waypoint
    # ----------------------------------------------------------------
    progress_task  = asyncio.ensure_future(print_mission_progress(drone))
    avoidance_task = asyncio.ensure_future(monitor_obstacles(drone, stop_lidar))
    complete_task  = asyncio.ensure_future(
        observe_mission_complete(drone, [progress_task, avoidance_task], stop_lidar)
    )

    try:
        # Run until the mission completes naturally
        await complete_task
        
    except (asyncio.CancelledError, KeyboardInterrupt):
        # This triggers if you press Ctrl+C to kill the script!
        log.warning("[Main] Script interrupted! Triggering emergency stop...")
        
    finally:
        log.info("[Main] Cleaning up flight controller state...")
        
        # 1. Zero out manual control inputs just in case we were reversing
        try: 
            await drone.manual_control.set_manual_control_input(0.0, 0.0, 0.0, 0.0)
        except: 
            pass
        
        # 2. Zero out offboard velocities and stop offboard mode
        try: 
            await drone.offboard.set_velocity_body(VelocityBodyYawspeed(0.0, 0.0, 0.0, 0.0))
            await drone.offboard.stop()
        except: 
            pass

        # 3. Force Hold mode (applies the brakes)
        try: 
            await drone.action.hold()
            log.info("[Main] Rover put into HOLD mode.")
        except Exception as e: 
            log.error("[Main] Could not hold: %s", e)
        
        # 4. Disarm the motors entirely
        try: 
            await drone.action.disarm()
            log.info("[Main] Rover disarmed.")
        except Exception as e: 
            log.error("[Main] Could not disarm: %s", e)

        # 5. Safely spin down the LiDAR
        stop_lidar.set()
        lidar_thread.join(timeout=5)
        log.info("[Main] LiDAR thread stopped. Program closed safely.")


if __name__ == "__main__":
    asyncio.run(main())
    
