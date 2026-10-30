# Python test port

> **TL;DR**
> - Where each of the Python service's 397 tests (`tests/collector`, `tests/runtime`, `tests/test_layers.py`)
>   went when the Python server was removed after 1.6.0 (remaining-work.md, "Python server removal").
> - 250 are Rust unit tests now, 105 are shown by the recorded comparisons or the live checks, 32 test
>   Python-only API, 5 stay with the CLI, and 5 cannot be made in a test (each says why).
> - The Rust tests read fake trees through `sysfs::at` (identity outside tests): `cargo test` in
>   `native/build.sh` runs them.

The Python service's own tests went with it; they are in the git history up to 1.6.0. So that what they
showed is not lost, every one of them is listed below with where it is shown now:

| mark | meaning |
| --- | --- |
| R | a Rust unit test: the file (`native/src/...`) and, where the name differs, the test |
| G | shown by a comparison with the Python service's recorded answers (`native/tools/equivalence/golden.py`: `fixture.py`'s fake-tree scenarios, `runtime_fixture.py`'s runtime scenarios, `config_fixture.py`) or by `live_http.py` on a real host; the scenario and step are named |
| P | tests the Python implementation itself (its call signatures, dict ownership, exceptions, threads): there is no native counterpart to test |
| K | tests `frontends/cli.py`, which stays: the test is in `tests/frontends/test_cli.py` |
| X | the behavior is the native server's too, but a test cannot produce the condition; the reason is given |

The Rust tests' fake trees: `sysfs::fake::Root` is a temporary directory that the test thread's reads
see as `/` (`sysfs::at` maps every path the Linux collectors open; it is the identity outside tests), with
the clocks set (`metadata::set_clocks`, per thread in tests). Where Python patched `open()`,
`os.listdir` or `socket.if_nameindex`, the tests give `fake::answers` (a read's bytes or errno),
`fake::unlistable` and `fake::indexes`. The tests run with the build (`native/build.sh`, `cargo test`).

`live_http.py` compared the two servers on a host; since the removal it checks the native server alone, against
the Python service's answers where they do not depend on the host (status codes, content types, the
raw requests' answers) and `frontends/cli.py`'s renderer for `/text`.

## `tests/collector/test_battery.py`

R 13

| test | mark | where |
| --- | --- | --- |
| `test_charge_family_battery` | R | observe/battery.rs |
| `test_energy_family_battery_keeps_the_sign` | R | observe/battery.rs |
| `test_an_attribute_the_driver_does_not_expose_is_not_a_problem` | R | observe/battery.rs |
| `test_no_supply_is_unavailable_not_an_error` | R | observe/battery.rs |
| `test_empty_bay_reports_absent_without_inventing_numbers` | R | observe/battery.rs |
| `test_external_supplies_and_their_order` | R | observe/battery.rs |
| `test_a_ups_holds_charge_like_a_battery` | R | observe/battery.rs |
| `test_a_supply_without_a_type_is_left_out` | R | observe/battery.rs |
| `test_a_value_that_is_not_one_is_invalid_data` | R | observe/battery.rs |
| `test_an_over_long_enum_is_not_one` | R | observe/battery.rs |
| `test_an_unreadable_attribute_is_reported_and_the_rest_kept` | R | observe/battery.rs |
| `test_the_host_class_reads` | R | observe/battery.rs |
| `test_config_section` | R | config.rs battery_section |

## `tests/collector/test_common.py`

R 42, G 1, P 6

| test | mark | where |
| --- | --- | --- |
| `test_cpu_percent` | R | collect/mod.rs cpu_percent_table (Python's 5-counter readings padded to 8) |
| `test_first_reading_warms_up_then_measures` | R | collect/mod.rs |
| `test_cores_come_and_go` | R | collect/mod.rs |
| `test_counter_regression_is_dropped_and_rebased` | R | collect/mod.rs |
| `test_no_ticks_is_not_zero` | R | collect/mod.rs |
| `test_invalid_interval` | R | collect/mod.rs |
| `test_gap_boundary_and_recovery` | R | collect/mod.rs |
| `test_read_failure_raises_and_restarts_warmup` | R | collect/mod.rs |
| `test_cpu_times_rejects_broken_readings` | R | common.rs |
| `test_cpu_times_reads_eight_or_more_counters` | R | common.rs |
| `test_cpu_percent_does_not_cut_readings_short` | P | Python's cpu_percent API: the native readings come from cpu_times, which refuses fewer than 8 counters (common.rs cpu_times_rejects_broken_readings) |
| `test_broken_reading_is_not_a_baseline` | R | common.rs a_broken_reading_is_not_a_baseline |
| `test_counters_are_independent` | R | collect/mod.rs |
| `test_detect` | R | common.rs detect_the_board |
| `test_hwmon_rails_and_pwm_only_fan` | R | common.rs |
| `test_hwmon_pc` | R | common.rs |
| `test_hwmon_twin_of_a_zone_is_skipped_by_its_path` | R | common.rs |
| `test_thermal_zones` | R | common.rs thermal_zone_types_and_paths |
| `test_host_paths_from_env` | R | common.rs host_paths_from_the_host_root (the paths under the root; the variables themselves are read by sysfs::host_root and device_tree) |
| `test_os_release` | R | common.rs os_release_names |
| `test_hwmon_same_names_kept_apart` | R | common.rs |
| `test_hwmon_unreadable_fan_is_none_not_zero` | R | common.rs |
| `test_service_path_reads_once_and_does_not_wait` | G | fixture pc (warmup, then interval readings); the native collection has no sleep and one /proc/stat read by construction |
| `test_oneshot_does_not_touch_the_service_baseline` | P | Python's one-shot collect() without counters: the native server has no one-shot mode |
| `test_direct_collect_is_a_oneshot_reading` | P | Python's one-shot collect() |
| `test_oneshot_single_cpu_iowait_drop` | P | Python's one-shot collect() |
| `test_oneshot_reports_unusable_cpus` | P | Python's one-shot collect() |
| `test_group_read_values` | R | common.rs |
| `test_group_read_errors` | R | common.rs |
| `test_group_read_missing_file` | R | common.rs |
| `test_group_status` | R | common.rs |
| `test_group_issues_are_capped` | R | common.rs |
| `test_one_bad_temperature_keeps_the_rest` | R | common.rs |
| `test_partial_power_and_fan_channels` | R | common.rs |
| `test_channel_that_disappears_then_recovers` | R | common.rs (a dangling link stands for a channel gone between the listing and the read) |
| `test_unlistable_hwmon_class` | R | common.rs |
| `test_inactive_thermal_zone_is_no_data` | R | common.rs |
| `test_fan_speed_the_firmware_does_not_report_is_no_data` | R | common.rs |
| `test_unexpected_error_in_one_group_is_internal_error` | P | Python exception path: the native readers return errors, they raise none |
| `test_cpu_frequency_failure_keeps_usage` | R | common.rs |
| `test_no_optional_sensors_is_not_an_error` | R | common.rs |
| `test_problems_are_logged_when_they_change` | R | common.rs |
| `test_undecodable_temperature_keeps_the_rest` | R | common.rs |
| `test_unlistable_chip_is_reported_and_the_others_kept` | R | common.rs |
| `test_unlistable_thermal_class` | R | common.rs |
| `test_empty_or_missing_classes_are_not_errors` | R | common.rs |
| `test_unreadable_rail_label_is_reported` | R | common.rs |
| `test_undecodable_name_or_label_keeps_the_values` | R | common.rs |
| `test_undecodable_zone_type_keeps_the_zones` | R | common.rs |

## `tests/collector/test_disk_io.py`

R 21, G 6, P 1

| test | mark | where |
| --- | --- | --- |
| `test_only_physical_disks_with_rates_over_the_observed_window` | R | passive.rs |
| `test_confirmed_kinds_are_reused_and_missing_links_retried` | R | passive.rs |
| `test_any_counter_going_down_gives_no_rate_but_in_flight_may` | R | passive.rs |
| `test_gap_boundary_and_bad_intervals` | R | passive.rs |
| `test_a_new_device_number_under_the_same_name_starts_over` | R | passive.rs |
| `test_bad_rows_are_left_out_and_others_kept` | R | passive.rs |
| `test_rows_of_left_out_devices_are_not_converted_or_reported` | R | passive.rs |
| `test_a_broken_file_gives_nothing_and_clears_baselines` | R | passive.rs |
| `test_unreadable_file` | R | passive.rs |
| `test_no_physical_disk_is_not_detected_not_an_error` | R | passive.rs |
| `test_changing_the_output_does_not_touch_the_baseline` | R | passive.rs |
| `test_is_physical_follows_the_sysfs_link` | R | passive.rs |
| `test_real_host_reading` | R | passive.rs |
| `test_changed_device_number_rechecks_classification` | R | passive.rs |
| `test_missing_sysfs_entry_is_retried_while_diskstats_name_remains` | R | passive.rs |
| `test_file_failure_discards_classification_cache` | R | passive.rs |
| `test_a_failed_lookup_is_reported_and_retried` | R | passive.rs (a readlink that fails with EINVAL; the reason mapping is sysfs.rs reasons_of_os_errors) |
| `test_many_devices_one_read_and_capped_issues` | R | passive.rs many_devices_and_capped_issues (one read per collection by construction) |
| `test_an_unexpected_error_clears_the_baseline_and_is_raised` | P | Python exception path: the Rust sample has no exception to raise |
| `test_on_unless_disabled` | R | passive.rs on_unless_disabled_and_disabled_never_reads; main.rs passes the [disk_io] flag |
| `test_disabled_never_reads` | R | passive.rs on_unless_disabled_and_disabled_never_reads |
| `test_shipped_ini_matches_the_default` | G | config_fixture (the shipped platmon.ini) |
| `test_direct_collect_without_disk_io_is_unchanged` | R | passive.rs on_unless_disabled_and_disabled_never_reads |
| `test_service_publishes_rates_with_their_read` | G | runtime provenance (all records check out), core (published) |
| `test_disk_failure_keeps_the_core_going` | G | runtime core (an optional group error, core listed: degraded) |
| `test_disk_read_counts_for_the_data_age` | G | runtime provenance |
| `test_a_bad_disk_read_falls_back` | G | runtime provenance (a reversed span, a number whose read is outside, disk_io without a read) |
| `test_network_and_disk_reads_are_checked_apart` | G | runtime provenance (network values without their read, pressure's read outside) |

## `tests/collector/test_gpu.py`

R 10, G 7

| test | mark | where |
| --- | --- | --- |
| `test_the_first_reading_has_no_share_and_the_next_one_has` | R | accel.rs gpu_tests |
| `test_the_share_is_the_time_the_gpu_was_not_idle` | R | accel.rs gpu_tests |
| `test_a_counter_reset_or_a_long_break_is_not_averaged_over` | R | accel.rs gpu_tests |
| `test_amdgpu_publishes_the_share_itself` | R | accel.rs gpu_tests |
| `test_a_card_this_module_cannot_read_is_still_named` | R | accel.rs gpu_tests |
| `test_no_card_at_all_is_not_detected` | R | accel.rs gpu_tests |
| `test_connectors_are_not_cards_and_the_first_readable_card_wins` | R | accel.rs gpu_tests |
| `test_an_unreadable_attribute_is_a_failure_and_keeps_the_rest` | R | accel.rs gpu_tests |
| `test_each_value_carries_where_it_was_read` | R | accel.rs gpu_tests |
| `test_the_collector_uses_it_and_a_board_source_comes_first` | G | fixture pc (DRM), jetson (the board's GPU first) |
| `test_nvidia_with_no_drm_or_an_unreadable_card` | G | fixture nvidia_alone, nvidia (sources, arguments, the 0.5 s limit) |
| `test_nvidia_does_not_replace_readable_drm` | G | fixture nvidia_drm_first (never run) |
| `test_nvidia_unavailable_and_invalid_fields` | G | fixture nvidia; nvidia.rs clocks_in_hertz_like_python_or_refused |
| `test_nvidia_bad_or_empty_response` | G | fixture nvidia (no rows, three fields, not a GPU UUID, a quote not closed) |
| `test_nvidia_failed_query_does_not_reuse_values` | G | fixture nvidia (past the limit, exit 3, killed, not executable, removed); nvidia.rs a_slow_query_is_killed_and_reaped |
| `test_nvidia_selection_and_identity_survive_query_order` | G | fixture nvidia (two GPUs: the smallest UUID; rows ended by a lone CR) |
| `test_nvidia_wsl_discovery_without_path` | R | nvidia.rs |

## `tests/collector/test_jetson.py`

R 18

| test | mark | where |
| --- | --- | --- |
| `test_power_mode` | R | jetson.rs |
| `test_tach_fans` | R | jetson.rs |
| `test_gpu` | R | jetson.rs |
| `test_l4t` | R | jetson.rs |
| `test_gpu_across_bsps` | R | jetson.rs |
| `test_gpu_load_fallback_hides_failed_candidates` | R | jetson.rs |
| `test_gpu_clock_failure_keeps_usage` | R | jetson.rs |
| `test_gpu_without_load` | R | jetson.rs |
| `test_extend_power_mode_and_l4t` | R | jetson.rs |
| `test_extend_bad_power_mode_and_l4t_do_not_stop_the_rest` | R | jetson.rs |
| `test_extend_without_nvpmodel` | R | jetson.rs |
| `test_tach_fan_unreadable_rpm` | R | jetson.rs |
| `test_gpu_undecodable_candidate_falls_back` | R | jetson.rs |
| `test_gpu_unlistable_devfreq_without_fallback` | R | jetson.rs |
| `test_gpu_matrix_a_failed_load_candidate_hidden` | R | jetson.rs |
| `test_gpu_matrix_b_unlistable_devfreq_with_load` | R | jetson.rs |
| `test_gpu_matrix_c_no_devfreq_is_not_a_warning` | R | jetson.rs |
| `test_extend_undecodable_nvpmodel_conf_keeps_the_mode_id` | R | jetson.rs |

## `tests/collector/test_network.py`

R 20, P 2, X 1

| test | mark | where |
| --- | --- | --- |
| `test_warmup_then_rates_over_the_observed_window` | R | passive.rs |
| `test_rate_uses_the_read_start_and_integer_ns` | R | passive.rs |
| `test_wall_clock_is_not_used` | R | passive.rs |
| `test_any_counter_going_down_gives_no_rate_and_a_new_baseline` | R | passive.rs |
| `test_gap_boundary_and_bad_intervals` | R | passive.rs |
| `test_interfaces_come_go_and_are_renamed_or_reindexed` | R | passive.rs |
| `test_namespace_change_restarts_every_baseline` | R | passive.rs |
| `test_identity_lookup_failures_keep_counters_without_rates` | R | passive.rs |
| `test_bad_rows_are_left_out_and_others_kept` | R | passive.rs |
| `test_a_malformed_duplicate_still_makes_the_name_ambiguous` | R | passive.rs |
| `test_bad_rows_in_the_counters_report_and_keep_the_rest` | R | passive.rs |
| `test_a_broken_file_gives_nothing_and_clears_baselines` | R | passive.rs |
| `test_unreadable_file` | R | passive.rs |
| `test_an_empty_list_is_not_a_broken_file` | R | passive.rs |
| `test_changing_the_output_does_not_touch_the_baseline` | R | passive.rs |
| `test_real_host_reading` | R | passive.rs |
| `test_namespace_a_b_a_and_a_failure_in_between` | R | passive.rs |
| `test_the_namespace_id_is_kept_per_instance` | R | passive.rs |
| `test_counter_accepts_exactly_what_it_did` | R | passive.rs |
| `test_many_interfaces_one_read_and_capped_issues` | R | passive.rs many_interfaces_and_capped_issues (one read per collection by construction) |
| `test_an_unexpected_error_clears_the_baseline_and_is_raised` | P | Python exception path: the Rust sample has no exception to raise |
| `test_namespace_is_looked_up_every_time_and_hashed_once` | P | Python's patched namespace_id; the lookup on every reading is passive.rs namespace_a_b_a_and_a_failure_in_between, the id kept per (dev, ino) is Network.ns |
| `test_same_inode_on_another_device_is_another_namespace` | X | a test cannot make a file with a given inode on another device; namespace_id hashes both (passive.rs netns_id_matches_python) |

## `tests/collector/test_network_integration.py`

R 8, G 11, P 3

| test | mark | where |
| --- | --- | --- |
| `test_on_unless_disabled_and_kept_by_the_service` | R | passive.rs network_on_unless_disabled_and_never_read_when_off; main.rs passes the [network] flag |
| `test_disabled_never_reads` | R | passive.rs network_on_unless_disabled_and_never_read_when_off |
| `test_bad_network_config_is_refused` | R | config.rs network_section; config_fixture |
| `test_network_default_is_on` | R | config.rs network_section |
| `test_direct_collect_without_network_is_unchanged` | R | passive.rs network_on_unless_disabled_and_never_read_when_off |
| `test_direct_collect_with_network_has_no_read_time` | R | passive.rs network_on_unless_disabled_and_never_read_when_off |
| `test_service_publishes_rates_with_their_read` | G | runtime provenance (all records check out); fixture passive (rates) |
| `test_network_failure_keeps_the_core_going` | G | runtime core (an optional group error: degraded); fixture passive_failures; logging once: passive.rs a_lasting_failure_is_logged_once |
| `test_unexpected_network_error_is_contained` | P | Python exception path (internal_error): the Rust sample has no exception to raise |
| `test_core_failure_keeps_the_last_good_network_and_reads_nothing` | R | passive.rs a_core_failure_reads_no_network; the kept sample and the 503: runtime core (failed, the last good one is stale) |
| `test_a_dropped_slow_collection_still_is_the_next_baseline` | G | runtime core (dropped: stale, no sample); the counters' baseline is the collector's own, which the Sampler does not see |
| `test_network_read_counts_for_the_data_age` | G | runtime provenance |
| `test_a_bad_network_read_falls_back` | G | runtime provenance (a reversed span, a span of floats, a number whose read is outside, read records on another clock) |
| `test_a_failed_network_reading_does_not_move_the_others_to_the_fallback` | G | runtime provenance (disk_io without a read, but no disks) |
| `test_a_network_record_without_network_falls_back` | G | runtime provenance (a network read without network values) |
| `test_unusable_records_never_publish_the_collected_read` | G | runtime provenance (required not a dict, sensor records of the wrong type) |
| `test_constructor_without_network_is_compatible` | P | Python's Collected constructor |
| `test_same_sample_polled_again_and_copies_are_independent` | G | runtime core (the same answers, byte for byte, as the time moves); the native Sampler serializes what it published |
| `test_serializing_while_collecting` | G | runtime.rs readers_are_not_held_up_while_a_record_is_serialized; http.rs concurrent_reads_never_collect |
| `test_http_answers_with_network` | G | runtime text; live_http.py (paths, types, Cache-Control, 404) |
| `test_generic_callable_with_a_network_key_is_passed_through` | P | Python's Sampler takes any collect function |
| `test_real_service_reading` | R | passive.rs real_service_reading |

## `tests/collector/test_npu.py`

R 7, G 1

| test | mark | where |
| --- | --- | --- |
| `test_the_first_reading_has_no_share_and_the_next_one_has` | R | accel.rs |
| `test_the_share_is_the_time_the_npu_worked` | R | accel.rs |
| `test_a_counter_reset_or_a_long_break_is_not_averaged_over` | R | accel.rs |
| `test_an_accelerator_this_module_cannot_read_is_still_named` | R | accel.rs |
| `test_a_host_without_an_accelerator_is_not_detected` | R | accel.rs |
| `test_an_unreadable_attribute_is_a_failure_and_keeps_the_rest` | R | accel.rs |
| `test_each_value_carries_where_it_was_read` | R | accel.rs |
| `test_the_collector_reports_it_as_its_own_group` | G | fixture pc |

## `tests/collector/test_pressure.py`

R 10, G 1

| test | mark | where |
| --- | --- | --- |
| `test_parse_some_and_full` | R | passive.rs |
| `test_parse_refuses_other_formats` | R | passive.rs |
| `test_all_three_with_cpu_full_left_out` | R | passive.rs |
| `test_no_pressure_directory_opens_nothing` | R | passive.rs |
| `test_one_unreadable_file_keeps_the_others` | R | passive.rs |
| `test_all_unreadable_is_an_error_without_a_read` | R | passive.rs |
| `test_the_read_spans_the_three_files` | R | passive.rs |
| `test_disabled_reads_nothing` | R | passive.rs |
| `test_real_host_pressure` | R | passive.rs |
| `test_on_by_default_and_off_when_asked` | R | passive.rs disabled_reads_nothing; config_fixture (the enabled key) |
| `test_pressure_read_counts_for_the_data_age_and_falls_back_alone` | G | runtime provenance |

## `tests/collector/test_probe.py`

R 11, K 1

| test | mark | where |
| --- | --- | --- |
| `test_parse_targets` | R | config.rs parse_targets_like_python |
| `test_parse_targets_refuses` | R | config.rs parse_targets_like_python; config_fixture |
| `test_connect_to_loopback_listener` | R | observe/probe.rs |
| `test_refused_is_a_failure_not_a_time` | R | observe/probe.rs |
| `test_timeout_is_reported_without_a_time` | R | observe/probe.rs (a full accept queue instead of a patched select) |
| `test_observation_keeps_a_bounded_failure_window` | R | collect/probe.rs |
| `test_targets_are_probed_one_after_another` | R | collect/probe.rs |
| `test_socket_creation_error_does_not_escape` | R | observe/probe.rs a_socket_that_cannot_be_made_is_an_error |
| `test_socket_error_keeps_other_targets_and_failure_window` | R | collect/probe.rs |
| `test_config_refuses` | R | config.rs probe_config_refusals_and_defaults; config_fixture |
| `test_enabled_by_default_without_targets` | R | config.rs probe_config_refusals_and_defaults |
| `test_cli_lines` | K | tests/frontends/test_cli.py `test_probe_lines` |

## `tests/collector/test_provenance.py`

R 19, G 18, P 3, X 2

| test | mark | where |
| --- | --- | --- |
| `test_descriptor_id_is_fixed` | R | common.rs; metadata.rs source_id_matches_python |
| `test_label_change_keeps_id_and_channels_differ` | R | common.rs |
| `test_same_name_on_two_devices_differs` | R | common.rs |
| `test_renumbered_hwmon_keeps_device_id` | R | common.rs (two temporary roots, as Python's two access prefixes) |
| `test_two_hwmon_nodes_on_one_device_are_not_merged` | R | common.rs |
| `test_virtual_hwmon_and_thermal_zone_are_path_based` | R | common.rs |
| `test_class_alias_and_device_path_resolve_alike` | R | common.rs |
| `test_canonical_agrees_with_the_portable_way` | R | common.rs |
| `test_canonical_without_kernel_resolution` | R | common.rs canonical_agrees_with_the_portable_way: the portable way is canonical's own fallback when /proc/self/fd cannot be read |
| `test_a_node_removed_while_resolving_is_not_reported` | X | needs a hook between canonical's two resolutions; sysfs.rs resolves the root first and the path last, as Python does |
| `test_a_link_retargeted_while_resolving_gives_the_new_target` | X | needs a hook between canonical's two resolutions (as above) |
| `test_a_retargeted_link_resolves_to_its_new_device` | R | common.rs |
| `test_an_unresolved_path_recovers` | R | common.rs |
| `test_a_second_node_on_the_device_is_seen_when_it_comes_and_goes` | R | common.rs |
| `test_unresolved_source_keeps_the_value` | R | common.rs (a chip outside the sysfs root instead of a patched canonical) |
| `test_gpu_sources_follow_the_file_used` | R | common.rs |
| `test_label_read_later_does_not_refresh_the_number` | R | common.rs |
| `test_computed_and_fan_spans` | R | common.rs |
| `test_gpu_spans_skip_the_failed_candidate` | R | common.rs |
| `test_opens_are_the_same_with_records` | P | counts Python's open() calls: the native trace adds clock reads and path resolutions, no file reads (fixture.py compares the traced collections) |
| `test_pointers_resolve_in_the_output` | G | fixture pc, jetson, passive (sensor records and the values they point at, compared exactly) |
| `test_required_reads_are_named_and_timed` | G | fixture (the required reads, compared exactly) |
| `test_label_fallback_pointer` | G | fixture failures (a label that cannot be read) |
| `test_direct_collect_has_no_records` | P | Python's collect() without records |
| `test_data_age_numbers` | G | runtime provenance (all records check out); runtime core (data age at stale_after) |
| `test_a_sensor_read_before_the_required_ones_counts` | G | runtime provenance (all records check out: the oldest read is a sensor's, before the required ones) |
| `test_stale_boundary_on_read_basis` | G | runtime core (data age at stale_after: still current; one ns later: stale) |
| `test_records_that_do_not_match_fall_back` | G | runtime provenance (a record for a value that is not there, a number without its record, a record without a span) |
| `test_missing_required_reads_fall_back` | G | runtime provenance (a required read missing) |
| `test_a_deleted_required_read_falls_back` | G | runtime provenance (a required read missing, only the required reads) |
| `test_other_names_cannot_stand_in_for_required_reads` | G | runtime provenance (an extra required read) |
| `test_values_are_checked_against_the_stats_not_a_list` | G | runtime provenance (a number without its record) |
| `test_only_rfc6901_pointers_to_sensor_values_are_published` | R | runtime.rs only_rfc6901_pointers_to_sensor_values_are_published (check); runtime provenance |
| `test_malformed_records_keep_the_values` | G | runtime provenance (required not a dict, a record that is not a dict) |
| `test_writer_survives_bad_records_and_recovers` | G | runtime.rs publishes_fails_and_recovers; runtime provenance |
| `test_a_slow_log_write_does_not_block_status_or_stop` | P | Python's logging under the Sampler's lock: the native fallback is logged after publication (runtime.rs) |
| `test_records_on_another_clock_are_not_used` | G | runtime provenance (read records on another clock) |
| `test_too_slow_still_counts_the_whole_collection` | G | runtime core (taking exactly stale_after: kept; dropped) |
| `test_published_records_stay_and_plain_dicts_cannot_fake_them` | G | runtime core (failed: the last good one kept); Python's plain-dict collect function has no native counterpart |
| `test_wall_clock_steps_do_not_move_read_offsets` | G | runtime core (no collectors at all, odd wall times) |
| `test_service_end_to_end_reports_no_paths` | G | live_http.py; fixture (collectors compared exactly, with no paths in them) |
| `test_virtual_devices_are_path_based` | R | common.rs |

## `tests/collector/test_storage.py`

R 19, G 5, P 3, K 1, X 1

| test | mark | where |
| --- | --- | --- |
| `test_parse_mountinfo_escapes_optional_fields_and_bad_lines` | R | observe/storage.rs |
| `test_read_only_is_the_filesystem_not_this_process_mount` | R | observe/storage.rs |
| `test_parse_partitions` | R | observe/storage.rs parse_partitions_rows |
| `test_local_block_filesystems_once_each_with_all_mount_points` | R | observe/storage.rs (the kernel's statvfs of the test tree) |
| `test_a_measured_zero_is_zero` | X | statvfs is the kernel's: a test cannot make a filesystem of 0 blocks |
| `test_statvfs_failure_keeps_the_entry_without_numbers` | R | observe/storage.rs: ENOENT, EACCES and a replaced mount; EIO cannot be made with a test tree |
| `test_unreadable_mountinfo_or_partitions` | R | observe/storage.rs |
| `test_nothing_found_is_unavailable` | R | observe/storage.rs |
| `test_a_device_number_without_a_name_is_kept` | R | observe/storage.rs |
| `test_starting_then_observed_then_stale` | G | runtime observations (starting, at stale_after, stale) |
| `test_an_unexpected_error_is_the_groups_own` | G | runtime observations (battery raised: internal_error) |
| `test_a_blocked_observation_stalls_only_its_group` | G | slow.rs a_blocked_read_stalls_only_its_group; runtime observations (while it runs) |
| `test_the_view_is_new_and_the_observation_unchanged` | P | Python object identity: the Rust view is a new Value by construction |
| `test_start_twice_is_one_thread` | P | Python's Slow.start guard: serve starts each Rust group once |
| `test_storage_is_on_by_default_and_its_interval_is_checked` | R | config.rs storage_section |
| `test_slow_groups_and_history_follow_configuration` | R | main.rs slow_groups_and_history_follow_configuration |
| `test_observations_endpoint` | G | runtime observations; live_http.py |
| `test_real_host_observation` | R | observe/storage.rs |
| `test_http_answers_while_a_group_is_blocked_and_stale` | G | slow.rs a_blocked_read_stalls_only_its_group; runtime observations (observations while both run) |
| `test_cli_storage_lines_and_ages` | K | tests/frontends/test_cli.py `test_storage_lines_and_ages` |
| `test_only_local_filesystem_lines_are_converted` | R | observe/storage.rs parse_mountinfo_escapes_optional_fields_and_bad_lines (the native parser always filters) |
| `test_capacity_is_bound_to_verified_mount_descriptor` | R | observe/storage.rs statvfs_failure_keeps_the_entry_without_numbers (replaced) |
| `test_replaced_mount_has_no_capacity_and_an_explicit_reason` | R | observe/storage.rs statvfs_failure_keeps_the_entry_without_numbers (replaced) |
| `test_capacity_descriptor_closes_on_statvfs_failure` | P | Python's patched os.close: observe/storage.rs capacity closes its descriptor after every result by construction |
| `test_storage_skips_file_binds_before_a_directory` | R | observe/storage.rs |
| `test_only_file_binds_keep_unknown_capacity` | R | observe/storage.rs |
| `test_file_bind_fallback_keeps_descriptor_identity_check` | R | observe/storage.rs storage_skips_file_binds_before_a_directory (the tree's real device) |
| `test_capacity_does_not_require_directory_read_permission` | R | observe/storage.rs |
| `test_capacity_rejects_symlinks_and_regular_files` | R | observe/storage.rs capacity_rejects_symlinks_and_regular_files_and_other_filesystems |

## `tests/collector/test_wifi.py`

R 17, K 1

| test | mark | where |
| --- | --- | --- |
| `test_connected_rows_from_the_boards` | R | observe/wifi.rs |
| `test_unupdated_signal_does_not_determine_connection` | R | observe/wifi.rs |
| `test_a_value_without_dbm_is_not_called_dbm` | R | observe/wifi.rs |
| `test_noise_when_the_driver_gives_it` | R | observe/wifi.rs |
| `test_bad_rows_and_files` | R | observe/wifi.rs |
| `test_observation_states` | R | observe/wifi.rs |
| `test_real_host_file` | R | observe/wifi.rs |
| `test_observation_exports_known_interface_identity` | R | observe/wifi.rs |
| `test_unavailable_identity_is_not_invented` | R | observe/wifi.rs |
| `test_signal_not_updated_does_not_claim_disconnection` | R | observe/wifi.rs |
| `test_connection_states_are_not_inferred_from_signal` | R | observe/wifi.rs |
| `test_carrier_and_index_from_fake_sysfs` | R | observe/wifi.rs |
| `test_interface_replaced_during_carrier_read_is_unknown` | R | observe/wifi.rs |
| `test_raw_zero_and_invalid_signal_range` | R | observe/wifi.rs |
| `test_down_carrier_is_disconnected_without_issue` | R | observe/wifi.rs |
| `test_carrier_errors_are_not_all_treated_as_down` | R | observe/wifi.rs |
| `test_wifi_is_on_by_default_with_a_5_s_candidate_interval` | R | config.rs wifi_section |
| `test_cli_lines` | K | tests/frontends/test_cli.py `test_wifi_lines` |

## `tests/runtime/test_history.py`

R 11, G 10, P 2

| test | mark | where |
| --- | --- | --- |
| `test_the_gpu_is_one_series_and_a_card_without_a_load_is_a_gap` | R | history.rs |
| `test_battery_points_carry_the_charge_and_the_draw` | R | history.rs |
| `test_a_battery_observation_reaches_the_history` | R | history.rs |
| `test_concurrent_append_and_view` | R | history.rs |
| `test_missing_series_and_elapsed_gap_do_not_join_values` | R | history.rs |
| `test_low_observation_ids_append_once_independent_of_core_reads` | R | history.rs |
| `test_slow_history_releases_expired_points_after_retention` | R | history.rs |
| `test_series_slot_is_freed_before_accepting_replacement` | R | history.rs |
| `test_wifi_history_requires_namespace_and_index_and_breaks_disconnection` | R | history.rs |
| `test_storage_history_identity_and_independent_cadence` | R | history.rs |
| `test_point_and_series_limits_even_at_fastest_cadence` | R | history.rs |
| `test_series_ids_follow_identities_and_gaps_are_none` | G | runtime history (all series; new series, missing ones null); history.rs a_host_scope_names_interfaces_by_host_and_index_and_an_unknown_namespace_has_none |
| `test_a_new_namespace_or_index_is_a_new_series` | G | runtime history (another namespace, wlan0 gone, two more CPUs) |
| `test_points_are_bounded_and_old_series_go_away` | G | runtime history (70 s later: expired series dropped); history.rs point_and_series_limits_even_at_fastest_cadence |
| `test_series_limit_counts_what_it_drops` | G | runtime history (70 temperatures: the limit, by priority; dropped counted once each) |
| `test_every_cpu_core_keeps_its_series_when_sensors_outnumber_the_limit` | G | runtime history (8 CPUs: core drops its own least important; the kept ones keep their slots) |
| `test_view_bounds_prefix_seconds_and_points` | G | runtime history (a prefix, percent-encoded; the last 2.5 s; 3 points; 1 point) |
| `test_only_published_snapshots_append_and_reads_never_do` | G | runtime history (failed: no point); runtime core (views never collect) |
| `test_one_failed_core_attempt_breaks_the_line_on_recovery` | G | runtime history (after a failure: a break) |
| `test_a_failing_append_never_stops_the_core` | P | Python exception from on_publish: the native append returns, it raises nothing |
| `test_history_endpoint` | G | runtime history (HTTP answers); live_http.py (/api/history) |
| `test_downsampling_preserves_missing_bucket_and_newest` | G | runtime history (3 points: buckets with a null stay null) |
| `test_slow_callback_observes_publish_once_and_never_runs_under_lock` | P | Python's on_publish callback under the group lock: slow.rs appends to the history after publishing, once per observation (runtime observations, history: observation series) |

## `tests/runtime/test_platmon.py`

R 10, G 2

| test | mark | where |
| --- | --- | --- |
| `test_defaults` | R | config.rs defaults |
| `test_shipped_ini_matches_defaults` | G | config_fixture (the shipped platmon.ini; its recorded answer is the defaults') |
| `test_explicit_disabled_features_override_defaults` | R | config.rs |
| `test_file_overrides_defaults` | R | config.rs |
| `test_frontends_flag_enables_exactly_those` | R | config.rs; config_fixture (--frontends cases) |
| `test_rejects_bad_config` | R | config.rs rejects_bad_config (every case and message); config_fixture |
| `test_levels` | R | config.rs levels_and_the_board_temperature |
| `test_board_temperature_when_not_configured` | R | config.rs levels_and_the_board_temperature |
| `test_missing_file` | R | config.rs |
| `test_sigterm_exits_promptly` | R | live_http.py (SIGTERM: exit 0 within 2 s, before the restart) |
| `test_replay_config_bounds` | R | config.rs |
| `test_replay_wiring_is_independent_of_numeric_history` | G | runtime features_off (replay off, history on) and history/replay scenarios; main.rs reads the two flags apart |

## `tests/runtime/test_replay.py`

R 5, G 6, P 1

| test | mark | where |
| --- | --- | --- |
| `test_full_snapshot_provenance_and_stale_values_are_recoverable` | G | runtime replay (from the start: the record is /api/stats as published); live_http.py (a record's keys are /api/stats', sample.stale) |
| `test_pages_survive_new_publications_and_have_no_duplicates` | G | runtime replay (a page of 2, after 2, at the latest); live_http.py replay walk |
| `test_restart_ahead_and_empty_cursors` | G | runtime replay (ahead of it, another instance, no such group stream); live_http.py (409 after a restart) |
| `test_retention_expires_even_without_new_publications` | G | runtime replay (core 25 s later: stale; all expired: the cursor still moves on) |
| `test_shared_byte_and_record_caps` | R | replay.rs the_record_cap_holds; runtime replay (capacity evicted the oldest) |
| `test_oversized_middle_and_tail_are_reported_and_cursor_progresses` | R | replay.rs |
| `test_response_byte_budget_paginates_before_count_limit` | R | replay.rs the_response_byte_budget_paginates_before_the_count_limit (records near the real limit instead of a patched one) |
| `test_independent_observation_ids_complete_payloads_and_ages` | G | runtime replay (storage, wifi partial; wifi, 20 s later: stale) |
| `test_failed_core_attempt_marks_recovery_without_inventing_sample` | G | runtime replay (failed, too slow, recovered: gap_before) |
| `test_serialization_failure_records_loss_and_does_not_stop_live_or_history` | R | replay.rs a_serialization_failure_records_its_loss |
| `test_observation_failure_is_archived_with_diagnostics` | R | replay.rs an_observation_failure_is_archived |
| `test_reader_and_writer_do_not_share_mutable_payloads` | P | Python dict sharing: the native replay keeps serialized text and parses a copy per page |

## `tests/runtime/test_sampler.py`

R 4, G 19, P 3, X 1

| test | mark | where |
| --- | --- | --- |
| `test_latest_follows_collect` | G | runtime core (published, sequence 2); live_http.py (sequences rise, never go back) |
| `test_failed_collect_keeps_last_snapshot` | G | runtime core (current snapshot, failed attempt: degraded) |
| `test_no_snapshot_yet` | G | runtime core (no collection yet: starting); http.rs no_snapshot_is_503 |
| `test_stale_snapshot_is_not_served` | G | runtime core (the last good one is stale; one ns later: stale, stats withheld) |
| `test_default_stale_after` | R | runtime.rs default_stale_after_and_a_new_instance_each_time |
| `test_metadata_is_added_and_stats_are_unchanged` | G | runtime core (published: the sample's fields, byte for byte) |
| `test_polling_does_not_change_the_sample` | G | runtime core (views of one publication at other times) |
| `test_sequence_counts_published_snapshots_only` | G | runtime core (dropped: stale, no sample; failed; recovered, sequence 2) |
| `test_each_sampler_is_a_new_instance` | R | runtime.rs default_stale_after_and_a_new_instance_each_time |
| `test_wall_clock_steps_do_not_change_durations` | G | runtime core (no collectors at all, odd wall times) |
| `test_stale_boundary` | G | runtime core (data age at stale_after: still current; one ns later: stale) |
| `test_too_slow_result_is_not_published` | G | runtime core (taking exactly stale_after: kept; dropped) |
| `test_failure_keeps_last_good_then_recovers` | G | runtime core (failed, failed again, recovered); runtime.rs publishes_fails_and_recovers |
| `test_starting_status` | G | runtime core (no collection yet: starting) |
| `test_blocked_first_collect_turns_stale` | G | runtime core (while it runs: starting, collecting_for_ms; past stale_after: stale) |
| `test_returned_dicts_are_copies` | P | Python dict ownership: the native Sampler hands out serialized text |
| `test_reads_match_their_metadata_while_collecting` | G | runtime.rs readers_are_not_held_up_while_a_record_is_serialized (one capture per publication) |
| `test_start_twice_is_one_writer_and_restart_is_refused` | P | Python's Sampler.start: serve starts the native writer once |
| `test_concurrent_start_makes_one_writer` | P | Python's Sampler.start |
| `test_pick_clock` | G | platform/mod.rs the_elapsed_clocks_run_and_sleep_only_adds_to_boottime; runtime core (clock: boottime) |
| `test_pick_clock_when_boottime_fails` | X | a test cannot make CLOCK_BOOTTIME fail; metadata::clock_source falls back to monotonic, as pick_clock does |
| `test_cpu_baseline_follows_readings_not_publishes` | G | collect/mod.rs CpuCounters: its baseline is every reading's, whatever the Sampler publishes (runtime core: dropped) |
| `test_cpu_window_ignores_wall_clock_steps` | R | collect/mod.rs (CpuCounters reads the elapsed clock only; the tests hold the wall clock still) |
| `test_broken_cpu_reading_fails_the_collection` | R | common.rs a_broken_reading_is_not_a_baseline; runtime core (failed: not published) |
| `test_optional_collectors_are_kept_and_core_is_the_samplers` | G | runtime core (an optional group error, core listed: core is the Sampler's); runtime.rs publishes_fails_and_recovers |
| `test_status_from_optional_collectors` | G | runtime core (partial, error: degraded; unavailable is not degraded) |
| `test_all_optional_failing_still_publishes_then_required_failure_keeps_last_good` | G | runtime core; common.rs no_optional_sensors_is_not_an_error |

## `tests/runtime/test_server.py`

R 5, G 12, P 1

| test | mark | where |
| --- | --- | --- |
| `test_a_silent_client_is_dropped_instead_of_parking_its_thread` | G | http.rs a_silent_client_is_dropped; live_http.py (a request line cut off: closed after the 10 s request timeout) |
| `test_answering_is_not_bounded_by_the_request_deadline` | R | http.rs a_slow_but_steady_reader_gets_the_whole_answer |
| `test_response_reclaims_stalled_reader_but_allows_continued_progress` | R | http.rs a_reader_that_stops_is_dropped, a_slow_but_steady_reader_gets_the_whole_answer |
| `test_connection_limit_rejects_excess_and_reuses_released_slot` | G | http.rs excess_connections_are_closed; live_http.py (32 held, a request refused while full and answered after) |
| `test_connection_slot_is_released_when_thread_creation_fails` | P | Python's patched ThreadingHTTPServer.process_request |
| `test_routes` | G | live_http.py (every path: status, Content-Type, Cache-Control, the same bytes); runtime text (stats, observations, unknown, HEAD) |
| `test_brand_assets` | G | live_http.py (the web page and its files: the same bytes) |
| `test_web_script_and_styles` | G | live_http.py (the web page and its files: the same bytes) |
| `test_levels_reach_the_page` | G | runtime text (status with levels); live_http.py (the page, levels filled in) |
| `test_web_asset_paths_are_allowlisted` | R | http.rs web_asset_paths_are_allowlisted |
| `test_no_snapshot_yet_is_503` | G | http.rs no_snapshot_is_503; runtime text (no snapshot yet: 503) |
| `test_503_metadata` | G | runtime text (200 s later: the snapshot stale); http.rs no_snapshot_is_503 |
| `test_status_does_not_wait_for_the_first_snapshot` | G | runtime core (no collection yet: starting, from a view that never waits) |
| `test_polling_does_not_collect` | G | http.rs concurrent_reads_never_collect; live_http.py (no more collections than intervals) |
| `test_partial_snapshot_is_served_and_degraded` | G | runtime core (an optional group partial: degraded); runtime text |
| `test_replay_http_pagination_staleness_and_restart` | G | runtime replay; live_http.py (replay walk, 409 after a restart) |
| `test_replay_http_rejects_invalid_query` | R | http.rs replay_http_rejects_invalid_queries_and_needs_a_group |
| `test_replay_disabled_and_observation_group_required` | R | http.rs replay_http_rejects_invalid_queries_and_needs_a_group; runtime features_off (replay off); runtime replay (no group, not a group) |

## `tests/runtime/test_snapshot_copy.py`

G 6, P 5

| test | mark | where |
| --- | --- | --- |
| `test_stats_json_is_read_serialized` | G | runtime core (the /api/stats body byte for byte, key order included) |
| `test_callers_changing_nested_values_do_not_reach_others` | P | Python dict ownership: the native Sampler hands out serialized text |
| `test_the_collectors_own_dict_changed_later_does_not_reach_the_snapshot` | P | Python dict ownership: the native collection is moved into the Sampler |
| `test_serializing_does_not_change_the_snapshot` | P | Python dict ownership |
| `test_a_publish_during_serialization_does_not_mix_snapshots` | G | runtime.rs readers_are_not_held_up_while_a_record_is_serialized |
| `test_concurrent_requests_for_one_sequence` | G | http.rs concurrent_reads_never_collect; live_http.py (16 clients × 20 requests, all verified) |
| `test_status_and_absence_match_read` | G | runtime core (every state's /api/stats and /api/status answers, byte for byte) |
| `test_same_snapshot_twice_ages_move` | G | runtime core (views of one publication at other times) |
| `test_sensor_meta_ids_and_reads_match_and_stay_owned` | G | runtime provenance (sensor_meta of every case); fixture (the collections' records) |
| `test_generic_collector_values` | P | Python's collect() may return any object |
| `test_unserializable_values_fail_as_before` | P | Python's TypeError from json.dumps; the native replay refuses a non-finite number (replay.rs a_serialization_failure_records_its_loss) |

## `tests/test_layers.py`

P 2, K 2

| test | mark | where |
| --- | --- | --- |
| `test_lower_layers_do_not_import_upper_ones` | P | the Python service's layers: removed with them |
| `test_cli_imports_nothing_from_the_project` | K | tests/frontends/test_cli.py |
| `test_the_allowed_exception_is_still_used` | P | runtime/server.py's import of frontends.cli: removed with it |
| `test_cli_version_copy_matches_the_service` | K | tests/frontends/test_cli.py `test_cli_version_copy_matches_the_version_file` |
