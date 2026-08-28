using System;
using UnityEditor;
using UnityEditor.AssetImporters;
using UnityEngine;

namespace RotoWeave.Editor
{
    [CustomEditor(typeof(RotoWeaveCharacterImporter))]
    public sealed class RotoWeaveCharacterImporterEditor : ScriptedImporterEditor
    {
        public override void OnInspectorGUI()
        {
            serializedObject.Update();
            EditorGUILayout.LabelField("RotoWeave 角色", EditorStyles.boldLabel);
            EditorGUILayout.HelpBox(
                "修改导入设置后点击 Apply。重新导入会原位更新生成资源；请把自定义组件与覆盖放在 Prefab Variant 中。",
                MessageType.Info);
            EditorGUILayout.PropertyField(
                serializedObject.FindProperty("pixelsPerUnit"),
                new GUIContent("PPU Override", "0 使用 RotoWeave 导出的有效 PPU"));
            EditorGUILayout.PropertyField(serializedObject.FindProperty("filterMode"), new GUIContent("Filter Mode"));
            serializedObject.ApplyModifiedProperties();
            ApplyRevertGUI();

            EditorGUILayout.Space(10f);
            EditorGUILayout.LabelField("可编辑 Unity 资产", EditorStyles.boldLabel);
            EditorGUILayout.HelpBox(
                "提取独立 .anim、可写 AnimatorController 和已接线的 Prefab Variant。Clip 继续引用本角色包中的共享 Sprite；目标目录必须为空或不存在，且永不覆盖已有文件。",
                MessageType.Info);
            if (GUILayout.Button("创建可编辑副本…", GUILayout.Height(28f)))
            {
                try
                {
                    RotoWeaveCharacterEditableExtractor.CreateEditableCopy(
                        (RotoWeaveCharacterImporter)target);
                }
                catch (Exception exception)
                {
                    Debug.LogException(exception);
                    EditorUtility.DisplayDialog(
                        "创建可编辑副本失败",
                        exception.Message,
                        "确定");
                }
            }
        }
    }
}
