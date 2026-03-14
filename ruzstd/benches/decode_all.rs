use criterion::{black_box, criterion_group, criterion_main, Criterion};
use ruzstd::decoding::FrameDecoder;

fn criterion_benchmark(c: &mut Criterion) {
    let mut fr = FrameDecoder::new();
    let target_slice = &mut vec![0u8; 1024 * 1024 * 200];
    let src = include_bytes!("../decodecorpus_files/z000033.zst");

    c.bench_function("decode_all_slice", |b| {
        b.iter(|| {
            fr.decode_all(src, target_slice).unwrap();
        })
    });
}

#[cfg(feature = "bench_exports")]
fn bitreader_get_bits_benchmark(c: &mut Criterion) {
    use ruzstd::BitReader;

    let data: Vec<u8> = (0u8..=127).collect();

    c.bench_function("bitreader_get_bits", |b| {
        b.iter(|| {
            let mut br = BitReader::new(black_box(&data));
            let mut acc: u64 = 0;
            let widths: &[usize] = &[1, 4, 2, 8, 3, 6, 16, 5, 7, 11, 9, 13, 2, 1, 4, 8];
            let mut w = 0;
            while br.bits_left() > 0 {
                let n = widths[w % widths.len()].min(br.bits_left());
                acc = acc.wrapping_add(br.get_bits(n).unwrap());
                w += 1;
            }
            black_box(acc)
        })
    });
}

#[cfg(feature = "bench_exports")]
criterion_group!(benches, criterion_benchmark, bitreader_get_bits_benchmark);

#[cfg(not(feature = "bench_exports"))]
criterion_group!(benches, criterion_benchmark);

criterion_main!(benches);
