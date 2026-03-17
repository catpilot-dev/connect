"""handlers package — re-exports all handler functions for server.py compatibility."""

from .middleware import cors_middleware

from .auth import (
    handle_me,
    handle_auth,
    handle_devices,
    handle_device_get,
    handle_device_stats,
    handle_device_location,
    handle_storage,
    handle_device_info,
    handle_device_is_onroad,
    handle_device_reboot,
    handle_device_poweroff,
    handle_device_language,
)

from .routes import (
    handle_routes_list,
    handle_routes_segments,
    handle_preserved_routes,
    handle_route_get,
    handle_route_enrich,
    handle_route_files,
    handle_route_scan,
    handle_route_manifest,
    handle_share_signature,
    handle_route_delete,
    handle_route_note,
    handle_route_bookmark_add,
    handle_route_bookmark_update,
    handle_route_bookmark_delete,
    handle_route_preserve,
    handle_route_unpreserve,
    handle_route_download,
    handle_connectdata,
)

from .media import (
    handle_screenshot,
    handle_frame,
    handle_camera_segment,
    handle_qcamera_hls_manifest,
    handle_qcamera_hls_segment,
)

from .hud import (
    handle_hud_prerender,
    handle_hud_progress,
    handle_hud_cancel,
    handle_hud_video,
    handle_hud_stream_start,
    handle_hud_stream_stop,
    handle_hud_stream_status,
    handle_hud_stream_serve,
    handle_hud_stream_offer,
    handle_hud_stream_ws,
    handle_screencast_start,
    handle_screencast_seek,
    handle_screencast_pause,
    handle_screencast_resume,
    handle_screencast_stop,
    handle_screencast_status,
)

from .software import (
    handle_software_get,
    handle_software_check,
    handle_software_download,
    handle_software_install,
    handle_software_branch,
    handle_software_uninstall,
    handle_software_prepare_plugins,
    handle_venv_sync,
)

from .params import (
    handle_lateral_delay,
    handle_toggles_get,
    handle_toggles_set,
    handle_params_get,
    handle_params_set,
)

from .mapd import (
    handle_tile_list,
    handle_tile_download,
    handle_tile_progress,
    handle_tile_cancel,
    handle_tile_delete,
    handle_mapd_check_update,
    handle_mapd_update,
)

from .models import (
    handle_models_active,
    handle_models_list,
    handle_models_swap,
    handle_models_check_updates,
    handle_models_download,
)

from .ssh_keys import (
    handle_ssh_keys_get,
    handle_ssh_keys_set,
    handle_ssh_keys_delete,
    handle_webrtc,
    handle_webrtc_health,
)

from .dashboard import (
    handle_dashboard_telemetry,
    handle_dashboard_ws,
)

from .driving import (
    handle_driving_ws,
)

from .home import (
    handle_home_ws,
)

from .signals import (
    handle_signal_catalog,
    handle_signal_data,
    handle_signal_all,
)

from .plugins import (
    handle_plugins_get,
    handle_plugin_toggle,
    handle_plugin_param,
    handle_plugin_repo_get,
    handle_plugin_repo_set,
    handle_plugin_repo_install,
)

from .updates import (
    handle_updates_check,
    handle_updates_apply,
)

from .screenshots import (
    handle_screenshots_list,
    handle_screenshot_serve,
    handle_screenshot_delete,
    handle_screenshot_by_time,
)

from .stubs import (
    handle_stub_empty_array,
    handle_stub_error,
)

from .spa import handle_spa
