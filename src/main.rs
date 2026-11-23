//! Stdin/stdout framing keeps native codecs separate from history transactions.
use std::{
    env,
    error::Error,
    io::{self, Read},
    path::PathBuf,
};
use txcript::harness::{
    antigravity::{Antigravity, AntigravityStore},
    claude_code::ClaudeCode,
    codex::Codex,
    simple::Simple,
};
use txcript::{Store, TextCodec, convert};

fn run() -> Result<(), Box<dyn Error>> {
    let mut args = env::args().skip(1);
    let mode = args.next().ok_or("missing codec operation")?;
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    match mode.as_str() {
        "decode-codex" => print!(
            "{}",
            Simple::to_text(&convert::<Codex, Simple>(&Codex::from_text(&input)?)?)?
        ),
        "decode-claude" => print!(
            "{}",
            Simple::to_text(&convert::<ClaudeCode, Simple>(&ClaudeCode::from_text(
                &input
            )?)?)?
        ),
        "encode-claude" => print!(
            "{}",
            ClaudeCode::to_text(&convert::<Simple, ClaudeCode>(&Simple::from_text(&input)?)?)?
        ),
        "encode-codex" => print!(
            "{}",
            Codex::to_text(&convert::<Simple, Codex>(&Simple::from_text(&input)?)?)?
        ),
        "read-agy" => {
            let db = PathBuf::from(args.next().ok_or("missing conversation snapshot")?);
            let root = db
                .parent()
                .and_then(|d| d.parent())
                .ok_or("invalid snapshot path")?;
            let transcript = AntigravityStore::new(root).load(&db)?;
            print!(
                "{}",
                Simple::to_text(&convert::<Antigravity, Simple>(&transcript)?)?
            );
        }
        "stage-agy" => {
            let out = PathBuf::from(args.next().ok_or("missing empty staging directory")?);
            if !out.is_dir() || out.read_dir()?.next().is_some() {
                return Err("staging directory must be empty".into());
            }
            AntigravityStore::new(out).save(&convert::<Simple, Antigravity>(
                &Simple::from_text(&input)?,
            )?)?;
        }
        _ => return Err("unknown codec operation".into()),
    }
    Ok(())
}

fn main() {
    if let Err(e) = run() {
        eprintln!("transcript conversion failed: {e}");
        std::process::exit(1);
    }
}
