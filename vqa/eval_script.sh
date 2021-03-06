root_dir='experiments/important/0424_main_contribution_exp_answer_zs_3div4'
path1='vqa_vlmap_answer_d_qa_split_objattr_answer_3div4_genome_memft_check_all_answer_thres1_50000_thres2_-1_tf_record_memft_vfeat_bottomup_36_my_bf_or_wordset_sp_bs512_lr0.001_20180423-205117'
path2='vqa_vlmap_answer_d_qa_split_objattr_answer_3div4_genome_memft_check_all_answer_thres1_50000_thres2_-1_tf_record_memft_vfeat_bottomup_36_my_bf_only_sp_bs512_lr0.001_20180423-231715'
path3='vqa_vlmap_answer_d_qa_split_objattr_answer_3div4_genome_memft_check_all_answer_thres1_50000_thres2_-1_tf_record_memft_vfeat_bottomup_36_my_wordset_only_sp_bs512_lr0.001_20180424-124312'
path4='vqa_standard_d_qa_split_objattr_answer_3div4_genome_memft_check_all_answer_thres1_50000_thres2_-1_tf_record_memft_vfeat_bottomup_36_my_default_bs512_lr0.001_20180424-100715'
path5='vqa_vlmap_answer_noc_d_qa_split_objattr_answer_3div4_genome_memft_check_all_answer_thres1_50000_thres2_-1_tf_record_memft_vfeat_bottomup_36_my_noc_bf_or_wordset_4801_bs512_lr0.001_20180424-171148'

for path in $path1 $path2 $path3 $path4 $path5
do
    for model_num in 1 801 1601 2401 3201 4001 4801 5601 6401 7201
    do
        python vqa/evaler.py --checkpoint $root_dir/$path/model-$model_num --split test
    done
done

#python vqa/evaler.py --checkpoint experiments/important/0424_main_contribution_exp_answer_zs_3div4/vqa_vlmap_answer_d_qa_split_objattr_answer_3div4_genome_memft_check_all_answer_thres1_50000_thres2_-1_tf_record_memft_vfeat_bottomup_36_my_bf_or_wordset_sp_bs512_lr0.001_20180423-205117/model-4001
