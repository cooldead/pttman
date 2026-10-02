mod common;

use common::FakePactl;
use pttman::{
    cli::Overrides,
    config::Config,
    daemon::{Action, State},
};
use std::os::unix::fs::PermissionsExt;
use std::time::{Duration, Instant};

#[test]
fn press_sound_is_nonblocking_deduplicated_and_failure_does_not_prevent_mute() {
    let dir = tempfile::tempdir().unwrap();
    let player = dir.path().join("paplay");
    let log = dir.path().join("calls");
    std::fs::write(&player, "#!/bin/bash\nprintf '%s\\n' \"$3\" >> \"$PTTMAN_TEST_SOUND_LOG\"\nexec /usr/bin/sleep 0.15\n").unwrap();
    std::fs::set_permissions(&player, std::fs::Permissions::from_mode(0o755)).unwrap();
    // This integration-test binary has one test; its environment is process-local.
    let old_path = std::env::var_os("PATH").unwrap();
    std::env::set_var(
        "PATH",
        format!("{}:{}", dir.path().display(), old_path.to_string_lossy()),
    );
    std::env::set_var("PTTMAN_TEST_SOUND_LOG", &log);
    let pactl = FakePactl::default()
        .with_output(&["set-source-mute", "mic", "0"], "")
        .with_output(&["set-source-mute", "mic", "1"], "");
    let config = Config {
        source: Some("mic".into()),
        press_sound: Some("/a sound.wav".into()),
        ..Default::default()
    };
    let mut state = State::new(&config, &Overrides::default(), &pactl);
    state.run_action(&pactl, Action::Press).unwrap();
    let deadline = Instant::now() + Duration::from_secs(3);
    while !log.exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(5));
    }
    assert!(log.exists(), "player was not launched");
    state.run_action(&pactl, Action::Press).unwrap();
    state.run_action(&pactl, Action::Release).unwrap();
    assert_eq!(state.last_applied_mute.get("mic"), Some(&true));
    state.run_action(&pactl, Action::Press).unwrap();
    std::thread::sleep(Duration::from_millis(250));
    assert_eq!(std::fs::read_to_string(&log).unwrap(), "/a sound.wav\n");
    // Even after playback ends, a repeated key-down must not play again.
    state.run_action(&pactl, Action::Press).unwrap();
    assert_eq!(std::fs::read_to_string(&log).unwrap(), "/a sound.wav\n");
    state.run_action(&pactl, Action::Release).unwrap();
    state.run_action(&pactl, Action::Press).unwrap();
    std::thread::sleep(Duration::from_millis(250));
    assert_eq!(std::fs::read_to_string(&log).unwrap().lines().count(), 2);
    state.release_sound = Some("/release.wav".into());
    state.run_action(&pactl, Action::Release).unwrap();
    state.run_action(&pactl, Action::Release).unwrap();
    state.run_action(&pactl, Action::Press).unwrap();
    std::thread::sleep(Duration::from_millis(250));
    let sounds = std::fs::read_to_string(&log).unwrap();
    assert_eq!(
        sounds
            .lines()
            .filter(|line| *line == "/release.wav")
            .count(),
        1
    );
    assert_eq!(
        sounds
            .lines()
            .filter(|line| *line == "/a sound.wav")
            .count(),
        3
    );
    // A release cue must still play while the press cue is playing.
    state.run_action(&pactl, Action::Release).unwrap();
    std::thread::sleep(Duration::from_millis(250));
    state.run_action(&pactl, Action::Press).unwrap();
    state.run_action(&pactl, Action::Release).unwrap();
    state.run_action(&pactl, Action::Release).unwrap();
    std::thread::sleep(Duration::from_millis(250));
    let sounds = std::fs::read_to_string(&log).unwrap();
    assert_eq!(
        sounds
            .lines()
            .filter(|line| *line == "/release.wav")
            .count(),
        3
    );
    assert_eq!(
        sounds
            .lines()
            .filter(|line| *line == "/a sound.wav")
            .count(),
        4
    );
    std::fs::remove_file(player).unwrap();
    std::env::set_var("PATH", dir.path());
    state.run_action(&pactl, Action::Release).unwrap();
    state.run_action(&pactl, Action::Press).unwrap();
    state.run_action(&pactl, Action::Release).unwrap();
    assert_eq!(state.last_applied_mute.get("mic"), Some(&true));
    std::env::set_var("PATH", old_path);
}
