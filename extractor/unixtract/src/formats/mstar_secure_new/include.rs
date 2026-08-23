use binrw::BinRead;

pub static CHUNK_ID:  &[u8; 8] = b"MTK.....";
pub static CHUNK_END: &[u8; 8] = b".....mtk";

#[derive(BinRead)]
pub struct ChunkInfo {
    _chunk_id_head: [u8; 8],
    pub segment_size: u32,
    pub file_data_offset: u32,
    pub file_data_len: u32,
    _file_hash_offset: u32,
    _file_hash_len: u32,
    _download_buf: u32,
    _reserved: [u8; 88],
    _chunk_id_end: [u8; 8],
}