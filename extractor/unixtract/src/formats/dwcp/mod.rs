mod include;
use std::any::Any;
use crate::AppContext;
use crate::utils::aes::decrypt_aes128_cbc_nopad;

use std::path::Path;
use std::fs::{self, OpenOptions};
use std::io::{Cursor, Write};
use aes::cipher::consts::U16;
use aes_gcm::AesGcm;
use aes_gcm::aes::Aes128;
use aes_gcm::{aead::{Aead, KeyInit}, Nonce};
use binrw::BinReaderExt;

use crate::utils::common;
use include::*;

//because it uses a 16byte nonce for some reason
type Aes128Gcm16 = AesGcm<Aes128, U16>;

pub fn is_dwcp_file(app_ctx: &AppContext) -> Result<Option<Box<dyn Any>>, Box<dyn std::error::Error>> {
    let file = match app_ctx.file() {Some(f) => f, None => return Ok(None)};

    let header_magic = common::read_file(&file, 0, 4)?;
    if header_magic == b"DWCP" {
        Ok(Some(Box::new(())))
    } else {
        Ok(None)
    }
}

pub fn extract_dwcp(app_ctx: &AppContext, _ctx: Box<dyn Any>) -> Result<(), Box<dyn std::error::Error>> {
    let mut file = app_ctx.file().ok_or("Extractor expected file")?;

    let header: HeaderPlaintext = file.read_be()?;
    if header.version != 1 {
        return Err("Invalid header version".into());
    }
    println!("File info -\nFile size: {}\nBody size: {}\nVersion: {}.{}.{}",
            header.file_size, header.body_size, header.system_software_version[0], header.system_software_version[1], header.system_software_version[2]);

    let psp_key: [u8; 16] = app_ctx.keys.get_key_as_arr("DWCP_PSP_KEY", 0)?;

    let enc_header_size = header.file_size - header.body_size - 64 - 64 - 32;
    if enc_header_size % 48 != 0 {
        return Err("Encrypted header size must be multiple of metadata block".into());
    }
    let mut enc_header = common::read_exact(&mut file, enc_header_size as usize)?;
    enc_header = decrypt_aes128_cbc_nopad(&enc_header, &psp_key, &header.header_dec_iv)?;

    let _header_signature = common::read_exact(&mut file, 0x40)?;
    //

    let block_count = enc_header_size / 48;
    println!("\nBlock count: {}", block_count);

    let mut enc_hdr_reader = Cursor::new(enc_header);

    fs::create_dir_all(&app_ctx.output_dir)?;
    let output_path = Path::new(&app_ctx.output_dir).join("ota_update.zip");
    let mut out_file = OpenOptions::new().write(true).create(true).open(output_path)?;

    for i in 0..block_count {
        //handle last block
        let size = if i == block_count-1 && (header.body_size as usize % BLOCK_SIZE) < BLOCK_SIZE {
            header.body_size as usize % BLOCK_SIZE
        } else {
            BLOCK_SIZE
        };
        println!("  decrypting block {}/{}, size: {}...", i+1, block_count, size);

        //each block gets its own metadata block with key, nonce, tag
        let meta: MetadataBlock = enc_hdr_reader.read_be()?;
        
        let mut block = common::read_exact(&mut file, size)?;
        block.extend_from_slice(&meta.tag); //add tag

        let cipher = Aes128Gcm16::new(&meta.key.try_into().unwrap());
        let nonce = Nonce::try_from(&meta.nonce[..])?;

        let decrypted = cipher.decrypt(&nonce, block.as_ref())?;
        out_file.write_all(&decrypted)?;
    }

    Ok(())
}