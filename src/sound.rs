use std::path::Path;
use std::process::{Child, Command, Stdio};

/// Keep at most one player alive. Audio never delays a microphone command.
#[derive(Debug, Default)]
pub struct Player {
    child: Option<Child>,
}

impl Player {
    pub fn reap(&mut self) {
        if let Some(child) = self.child.as_mut() {
            match child.try_wait() {
                Ok(Some(status)) => {
                    if !status.success() {
                        tracing::warn!("PTT sound player exited with {}", status);
                    }
                    self.child = None;
                }
                Ok(None) => {}
                Err(err) => {
                    tracing::warn!("sound player status: {}", err);
                }
            }
        }
    }

    pub fn play(&mut self, path: &Path, volume: u8) {
        self.reap();
        if self.child.is_some() {
            tracing::info!("PTT sound skipped: previous sound still playing");
            return;
        }
        match playback_command(path, volume)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
        {
            Ok(child) => self.child = Some(child),
            Err(err) => tracing::warn!("could not play PTT sound: {}", err),
        }
    }
}

fn playback_command(path: &Path, volume: u8) -> Command {
    let volume = volume.min(100);
    let mp3 = path
        .extension()
        .is_some_and(|extension| extension.eq_ignore_ascii_case("mp3"));
    let mut command = Command::new(if mp3 { "ffplay" } else { "paplay" });
    if mp3 {
        command.args(["-nodisp", "-autoexit", "-loglevel", "error", "-volume"]);
        command.arg(volume.to_string()).arg("-i");
    } else {
        command
            .arg(format!(
                "--volume={}",
                (u32::from(volume) * 65536 + 50) / 100
            ))
            .arg("--");
    }
    command.arg(path);
    command
}

impl Drop for Player {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::playback_command;
    use std::path::Path;

    #[test]
    fn mp3_uses_ffplay_and_preserves_path_as_one_argument() {
        let command = playback_command(Path::new("/sounds/Push sound.MP3"), 35);
        assert_eq!(command.get_program(), "ffplay");
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            [
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-volume",
                "35",
                "-i",
                "/sounds/Push sound.MP3"
            ]
        );
        let command = playback_command(Path::new("/sounds/release.wav"), 50);
        assert_eq!(command.get_program(), "paplay");
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            ["--volume=32768", "--", "/sounds/release.wav"]
        );
    }
}
