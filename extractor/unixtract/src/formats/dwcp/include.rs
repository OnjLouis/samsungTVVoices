use binrw::BinRead;

pub static BLOCK_SIZE: usize = 0x100000;

#[derive(BinRead)]
pub struct HeaderPlaintext {
    _magic: [u8;4],
    pub version: u32,
    pub file_size: u64,
    pub body_size: u64,
    _attribute: u32,
    _reserved: u32,
    pub system_software_version: [u32;4],
    pub header_dec_iv: [u8;16],
}

#[derive(BinRead)]
pub struct MetadataBlock {
    pub key: [u8; 16],
    pub nonce: [u8; 16],
    pub tag: [u8; 16],
}